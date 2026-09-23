from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx

Progress = Callable[[str], None]

IMAGE_START_INTERVAL_SECONDS = 30.0
IMAGE_EOF_RETRY_COUNT = 1
IMAGE_EOF_RETRY_DELAY_SECONDS = 10.0


class PipelineError(RuntimeError):
    pass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


WEB_ROOT = Path(__file__).resolve().parent
BLOG_ROOT = Path(_env("BLOG_ROOT", str(WEB_ROOT.parent))).resolve()
SCRIPTS = WEB_ROOT / "scripts"
PHOTOS = BLOG_ROOT / "photos"
CHARACTER_REFERENCES = {
    "girl": "girl.png",
    "girl-baby": "girl-baby.png",
    "girl-old": "girl-old.png",
    "boy": "boy.png",
    "boy-baby": "boy-baby.png",
    "boy-old": "boy-old.png",
}
DEFAULT_CHARACTER = "girl"
CHARACTER_VALUES = tuple(CHARACTER_REFERENCES)
DEFAULT_SCENE_CHARACTER_COUNT = 1
SCENE_TYPES = {"single", "mixed_pair", "romantic_pair", "family_trio", "other_group"}
AGE_GROUPS = {"child", "adult", "elder", "unknown"}
FEMALE_BY_AGE = {"girl": "girl", "girl-baby": "girl-baby", "girl-old": "girl-old"}
MALE_BY_AGE = {"boy": "boy", "boy-baby": "boy-baby", "boy-old": "boy-old"}
FEMALE_EQUIVALENTS = {**FEMALE_BY_AGE, "boy": "girl", "boy-baby": "girl-baby", "boy-old": "girl-old"}
MALE_EQUIVALENTS = {**MALE_BY_AGE, "girl": "boy", "girl-baby": "boy-baby", "girl-old": "boy-old"}


def _base_url() -> str:
    return _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def _endpoint(path: str) -> str:
    return f"{_base_url()}/{path.lstrip('/')}"


def _headers() -> dict[str, str]:
    key = _env("OPENAI_API_KEY", "")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"} if key else {"Accept": "application/json"}


def _postman_headers() -> list[dict[str, str]]:
    """Return importable headers without ever persisting the configured API key."""
    headers = [{"key": "Accept", "value": "application/json", "type": "text"}]
    if _env("OPENAI_API_KEY", ""):
        headers.insert(0, {"key": "Authorization", "value": "Bearer {{OPENAI_API_KEY}}", "type": "text"})
    return headers


def _shell_quote(value: object) -> str:
    """Quote one value for a cURL command that Postman can import."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _api_debug_dir(output: Path, request_dir: Path | None) -> Path:
    if request_dir is not None:
        return request_dir.resolve()
    # Normal pipeline outputs are work_dir/raw-cards/image-XXX.png.
    return (output.parent.parent / "api-debug").resolve()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _form_data_entries(fields: dict[str, str], file_specs: list[dict[str, str]]) -> list[dict[str, str]]:
    entries = [
        {"key": key, "value": value, "type": "text"}
        for key, value in fields.items()
    ]
    entries.extend(
        {"key": spec["field"], "src": spec["path"], "type": "file"}
        for spec in file_specs
    )
    return entries


def _curl_command(url: str, headers: list[dict[str, str]], fields: dict[str, str], file_specs: list[dict[str, str]]) -> str:
    parts = ["curl --request POST", f"--url {_shell_quote(url)}"]
    parts.extend(
        f"--header {_shell_quote(header['key'] + ': ' + header['value'])}"
        for header in headers
    )
    parts.extend(
        f"--form-string {_shell_quote(key + '=' + value)}"
        for key, value in fields.items()
    )
    parts.extend(
        f"--form {_shell_quote(spec['field'] + '=@' + spec['path'] + ';type=' + spec['content_type'])}"
        for spec in file_specs
    )
    return " ".join(parts) + "\n"


def _postman_collection(
    name: str,
    url: str,
    headers: list[dict[str, str]],
    fields: dict[str, str],
    file_specs: list[dict[str, str]],
    *,
    json_body: dict | None = None,
) -> dict:
    if json_body is not None:
        body = {"mode": "raw", "raw": json.dumps(json_body, ensure_ascii=False, indent=2), "options": {"raw": {"language": "json"}}}
    else:
        body = {"mode": "formdata", "formdata": _form_data_entries(fields, file_specs)}
    return {
        "info": {
            "name": f"Web-v3 image request - {name}",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [{"key": "OPENAI_API_KEY", "value": ""}],
        "item": [{
            "name": name,
            "request": {"method": "POST", "header": headers, "body": body, "url": url},
        }],
    }


def _save_image_api_request(
    output: Path,
    endpoint: str,
    model: str,
    prompt: str,
    fields: dict[str, str],
    file_specs: list[dict[str, str]],
    *,
    request_dir: Path | None = None,
    json_body: dict | None = None,
    attempt: int = 1,
) -> dict[str, Path]:
    debug_dir = _api_debug_dir(output, request_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    name = output.stem if attempt <= 1 else f"{output.stem}.retry-{attempt - 1}"
    request_path = debug_dir / f"{name}.request.json"
    postman_path = debug_dir / f"{name}.postman.json"
    curl_path = debug_dir / f"{name}.curl.txt"
    response_path = debug_dir / f"{name}.response.body"
    headers = _postman_headers()
    request_record = {
        "method": "POST",
        "url": endpoint,
        "headers": headers,
        "model": model,
        "prompt": prompt,
        "fields": fields,
        "files": file_specs,
        "response_body_file": str(response_path),
        "response_status": None,
        "response_headers": None,
        "note": "Authorization 使用 {{OPENAI_API_KEY}} 占位符；不要手动设置 multipart Content-Type。",
    }
    if json_body is not None:
        request_record["json_body"] = json_body
        curl = " ".join([
            "curl --request POST",
            f"--url {_shell_quote(endpoint)}",
            *[f"--header {_shell_quote(header['key'] + ': ' + header['value'])}" for header in headers],
            f"--header {_shell_quote('Content-Type: application/json')}",
            f"--data-raw {_shell_quote(json.dumps(json_body, ensure_ascii=False))}",
        ]) + "\n"
    else:
        request_record["form_data"] = _form_data_entries(fields, file_specs)
        curl = _curl_command(endpoint, headers, fields, file_specs)
    _write_json(request_path, request_record)
    _write_json(postman_path, _postman_collection(name, endpoint, headers, fields, file_specs, json_body=json_body))
    curl_path.write_text(curl, encoding="utf-8", newline="\n")
    return {"request": request_path, "postman": postman_path, "curl": curl_path, "response": response_path}


def _record_image_api_response(
    request_path: Path,
    response_path: Path,
    response: httpx.Response,
) -> None:
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_bytes(response.content)
    try:
        record = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    record["response_status"] = response.status_code
    record["response_headers"] = dict(response.headers)
    _write_json(request_path, record)


def _response_preview(response: httpx.Response, limit: int = 4000) -> str:
    try:
        body = response.text.strip()
    except Exception:
        body = ""
    if not body:
        return "<空响应 body>"
    if len(body) > limit:
        return body[:limit] + "…"
    return body


def _write_image_failure_log(
    work_dir: Path,
    generated: list[tuple[dict, Path, Path, Exception | None]],
    finalized: list[tuple[dict, Exception | None]],
) -> Path | None:
    failures: list[dict[str, object]] = []
    debug_dir = work_dir / "api-debug"
    for job, result in zip(generated, finalized):
        _, raw, _, _ = job
        block, error = result
        if error is None:
            continue
        request_paths = sorted(debug_dir.glob(f"{raw.stem}*.postman.json"))
        response_paths = sorted(debug_dir.glob(f"{raw.stem}*.response.body"))
        failures.append({
            "image": raw.stem,
            "sourceIndex": block.get("sourceIndex"),
            "error": str(error),
            "postmanRequest": str(request_paths[-1]) if request_paths else None,
            "responseBody": str(response_paths[-1]) if response_paths else None,
            "postmanRequests": [str(path) for path in request_paths],
            "responseBodies": [str(path) for path in response_paths],
        })
    if not failures:
        return None
    path = work_dir / "image-failures.json"
    _write_json(path, {"count": len(failures), "failures": failures})
    return path


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise PipelineError("模型没有返回有效的 JSON 文章计划。")

    candidate = cleaned[start:end + 1]
    parse_errors: list[json.JSONDecodeError] = []
    candidates = [candidate, _repair_json_syntax(candidate)]
    for option in candidates:
        for strict in (True, False):
            try:
                value = json.loads(option, strict=strict)
                break
            except json.JSONDecodeError as exc:
                parse_errors.append(exc)
        else:
            continue
        break
    else:
        exc = parse_errors[-1]
        context_start = max(0, exc.pos - 50)
        context_end = min(len(candidate), exc.pos + 50)
        context = candidate[context_start:context_end].replace("\n", "\\n")
        raise PipelineError(
            f"模型返回的 JSON 无法解析：{exc}；错误附近：{context}"
        ) from exc
    if not isinstance(value, dict):
        raise PipelineError("模型返回的文章计划必须是对象。")
    return value


def _repair_json_syntax(value: str) -> str:
    """Repair common model-only JSON mistakes without changing normal JSON."""
    repaired: list[str] = []
    in_string = False
    escaped = False
    length = len(value)
    index = 0
    while index < length:
        char = value[index]
        if char == "\\" and in_string:
            repaired.append(char)
            escaped = not escaped
            index += 1
            continue
        if char == '"':
            if not in_string:
                in_string = True
                repaired.append(char)
            elif escaped:
                repaired.append(char)
                escaped = False
            else:
                lookahead = index + 1
                while lookahead < length and value[lookahead].isspace():
                    lookahead += 1
                if lookahead >= length or value[lookahead] in ",:]}":
                    in_string = False
                    repaired.append(char)
                else:
                    repaired.append('\\"')
            index += 1
            continue
        repaired.append(char)
        escaped = False
        index += 1

    result = "".join(repaired)
    return re.sub(r",(\s*[}\]])", r"\1", result)


def _response_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    if chunks:
        return "\n".join(chunks)
    raise PipelineError("模型响应中没有文本输出。")


def _non_whitespace_count(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def _normalize_article_plan(
    source: dict,
    plan: dict,
    article_mode: str,
    image_mode: str,
    progress: Progress,
) -> dict:
    """Normalize model-only fields before any expensive image work starts.

    The source URL, source indexes and actual image/result relationship remain
    strict.  Fields that are presentation-only or commonly malformed in model
    JSON are repaired and recorded as warnings instead of discarding a task
    that has already completed most of its work.
    """
    warnings = [
        item for item in plan.get("warnings", [])
        if isinstance(item, str) and item.strip()
    ] if isinstance(plan.get("warnings"), list) else []
    source_blocks = [item for item in source.get("blocks", []) if isinstance(item, dict)]
    source_indexes = {
        item.get("sourceIndex") for item in source_blocks
        if isinstance(item.get("sourceIndex"), int) and not isinstance(item.get("sourceIndex"), bool)
    }
    source_images = {
        item.get("sourceIndex"): item for item in source_blocks
        if item.get("type") == "image" and isinstance(item.get("sourceIndex"), int)
    }

    title = plan.get("title")
    if not isinstance(title, str) or not title.strip():
        title = source.get("title")
        warnings.append("文章标题缺失，已回退使用原文标题。")

    summary = plan.get("summary")
    if summary is None:
        plan.pop("summary", None)
    elif not isinstance(summary, str):
        warnings.append("summary 不是字符串，已忽略。")
        plan.pop("summary", None)
    elif isinstance(summary, str) and not summary.strip():
        warnings.append("summary 为空，已按无导语处理。")
        plan.pop("summary", None)
    elif isinstance(summary, str):
        plan["summary"] = summary.strip()

    removed_raw = plan.get("removedSourceIndexes", [])
    if removed_raw is None:
        removed_raw = []
        warnings.append("removedSourceIndexes 为空，已按空数组处理。")
    elif not isinstance(removed_raw, list):
        removed_raw = []
        warnings.append("removedSourceIndexes 不是数组，已按空数组处理。")
    removed: list[int] = []
    for position, value in enumerate(removed_raw):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            warnings.append(f"removedSourceIndexes[{position}] 无效，已忽略。")
            continue
        if value not in source_indexes:
            warnings.append(f"removedSourceIndexes 包含不存在的 sourceIndex：{value}，已忽略。")
            continue
        if value in removed:
            warnings.append(f"removedSourceIndexes 重复包含 sourceIndex={value}，已去重。")
            continue
        removed.append(value)

    raw_blocks = plan.get("blocks", [])
    if raw_blocks is None:
        raw_blocks = []
        warnings.append("article-plan.json.blocks 缺失，按空数组处理。")
    elif not isinstance(raw_blocks, list):
        raise PipelineError("模型返回的文章计划 blocks 必须是数组。")

    normalized: list[dict] = []
    handled_images: set[int] = set()
    for position, block in enumerate(raw_blocks):
        if not isinstance(block, dict):
            warnings.append(f"article-plan.json.blocks[{position}] 不是对象，已跳过。")
            continue
        block_type = block.get("type")
        if block_type in {"text", "heading", "quote"}:
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                warnings.append(f"article-plan.json.blocks[{position}] 文本为空，已跳过。")
                continue
            item = {key: value for key, value in block.items() if key in {"type", "text", "level"}}
            if block_type == "heading":
                try:
                    raw_level = int(block.get("level", 2))
                except (TypeError, ValueError):
                    raw_level = 2
                level = 3 if raw_level == 3 else 2
                if block.get("level", 2) != level:
                    warnings.append(f"article-plan.json.blocks[{position}] 标题层级无效，已归一为 {level} 级。")
                item["level"] = level
            normalized.append(item)
            continue

        if image_mode == "comic" and block_type in {"image", "generatedImageLink"}:
            item = dict(block)
            _normalize_scene_metadata(item, position, warnings)
            if block_type == "generatedImageLink" and not str(item.get("error") or "").strip():
                item["error"] = "图片生成失败"
                warnings.append(f"comic 图片 blocks[{position}] 缺少失败说明，已补充通用说明。")
            normalized.append(item)
            continue
        if image_mode == "comic" and block_type == "sourceImageLink":
            item = dict(block)
            item["type"] = "generatedImageLink"
            item["error"] = str(item.get("error") or "图片生成失败")
            item.pop("sourceIndex", None)
            warnings.append(f"comic 图片 blocks[{position}] 使用了 sourceImageLink，已转为失败占位。")
            normalized.append(item)
            continue

        if image_mode != "comic" and block_type in {"image", "sourceImageLink"}:
            source_index = block.get("sourceIndex")
            if (
                isinstance(source_index, bool)
                or not isinstance(source_index, int)
                or source_index not in source_images
                or source_index in removed
            ):
                warnings.append(f"article-plan.json.blocks[{position}] 图片 sourceIndex 无效，已跳过。")
                continue
            if source_index in handled_images:
                warnings.append(f"来源图片 sourceIndex={source_index} 重复，已跳过重复项。")
                continue
            handled_images.add(source_index)
            item = dict(block)
            _normalize_scene_metadata(item, position, warnings)
            normalized.append(item)
            continue

        warnings.append(f"article-plan.json.blocks[{position}] 类型 {block_type!r} 不受支持，已跳过。")

    plan["title"] = title.strip() if isinstance(title, str) else ""
    plan["removedSourceIndexes"] = removed
    plan["blocks"] = normalized
    plan["articleMode"] = article_mode
    plan["imageMode"] = image_mode
    if warnings:
        plan["warnings"] = warnings
        for warning in warnings:
            progress(f"提示：{warning}")
    else:
        plan.pop("warnings", None)
    return plan


class OpenAICompatible:
    def __init__(self):
        self.text_model = _env("TEXT_MODEL", "gpt-5.6-luna")
        self.image_model = _env("IMAGE_MODEL", "gpt-image-2")
        self.timeout = float(_env("AI_TIMEOUT_SECONDS", "900"))

    def plan(self, source: dict, work_dir: Path, article_mode: str, image_mode: str, comic_count: int, caption_mode: str, progress: Progress, brand: str = "on") -> dict:
        if not _env("OPENAI_API_KEY", ""):
            raise PipelineError("Web-v3 未配置 OPENAI_API_KEY，请填写 web-v3/.env。")
        blocks = [block for block in source.get("blocks", []) if isinstance(block, dict)]
        source_text = "\n".join(
            f"[{block.get('sourceIndex')}] {block.get('type')}: {block.get('text', block.get('alt', ''))}"
            for block in blocks if image_mode != "comic" or block.get("type") == "text"
        )
        source_count = sum(_non_whitespace_count(str(block.get("text", ""))) for block in blocks if block.get("type") == "text")
        mode_instruction = {
            "trans": "深度原创重构标题、结构、论证顺序和表达；正文不设置字符数硬上限，不要因长度截断必要内容。",
            "ori": "只做二次语义清洗，正文按清洗后的原文顺序输出，不压缩、不改写、不执行原创字符数限制；必须保留来源正文中的显式换行，原文连续行要在 text 中使用 \\n 表示，不得合并成一段。",
        }[article_mode]
        if image_mode == "comic":
            image_instruction = f"只根据清洗后的文字拆分为恰好 {comic_count} 个连续叙事节点；不要读取、引用或输出任何来源图片 sourceIndex。"
            image_schema = '{"type":"image","sceneType":"single","character":"girl","characters":[{"role":"girl","purpose":"main","ageGroup":"unknown","ageEvidence":"年龄不明"}],"sceneCharacterCount":1,"secondaryCharacterNotes":"次要人物的差异化描述；单人物时为空","allowIntentionalDuplicate":false,"imagePrompt":"连续叙事画面提示词","alt":"第几幕"}'
        else:
            image_instruction = "每张未被 removedSourceIndexes 删除的来源图片都必须恰好返回一个 type=image 的生图计划，并按 sourceIndex 递增。sourceImageLink 只由程序在真实生图或合成失败后自动写入，规划阶段不要返回 sourceImageLink。"
            image_schema = '{"type":"image","sourceIndex":1,"sceneType":"single","character":"girl","characters":[{"role":"girl","purpose":"main","ageGroup":"unknown","ageEvidence":"年龄不明"}],"sceneCharacterCount":1,"secondaryCharacterNotes":"次要人物的差异化描述；单人物时为空","allowIntentionalDuplicate":false,"emotion":"通用","caption":"图片文案","imagePrompt":"详细生图提示词","alt":"图片说明"}'
        brand_instruction = (
            "使用项目角色参考图判断并返回 character、characters、ageGroup 和 ageEvidence；character/characters[].role 只能是 girl、girl-baby、girl-old、boy、boy-baby、boy-old，"
            "角色参考图输入顺序为 girl、girl-baby、girl-old、boy、boy-baby、boy-old；角色参考图只用于识别角色，不是最终图片的统一人物模板；"
            "ageGroup 只能是 child、adult、elder、unknown；无法判断主角年龄时使用 role=girl、ageGroup=unknown。emotion 只用于 local 模式配色。"
            if brand == "on" else "不使用任何品牌人物参考图；仍须根据画面语义判断并返回 sceneType、character、characters、ageGroup 和 ageEvidence，供生图 prompt 约束。"
        )
        prompt = f"""
你是 Blog Article Studio 的文章重构规划器。只返回 JSON，不要 Markdown 或解释。
文章模式：{article_mode}。图片模式：{image_mode}。文案模式：{caption_mode}。{mode_instruction}
{image_instruction}{brand_instruction}
JSON 结构：
{{
  "title": "标题",
  "summary": "可选导语（没有导语时省略，不要返回空字符串）",
  "removedSourceIndexes": [0],
  "blocks": [
    {{"type":"heading","level":2,"text":"章节标题"}},
    {{"type":"text","text":"文章正文"}},
    {image_schema}
  ]
}}
要求：删除作者、来源、二维码、广告、推广、联系方式和无关装饰；所有图片计划必须包含 sceneType、character、characters、sceneCharacterCount、secondaryCharacterNotes、allowIntentionalDuplicate；每个 characters 项必须包含 role、purpose、ageGroup、ageEvidence；brand on 时普通模式额外必须包含 emotion、caption、imagePrompt、alt，comic 图片必须包含 imagePrompt、alt。sceneType 只能是 single、mixed_pair、romantic_pair、family_trio、other_group；ageGroup 只能是 child、adult、elder、unknown。先判断是否存在孩子、青年/成年人、爷爷奶奶辈，是否为父母、夫妻、情侣或祖孙关系，再判断每个人的 ageGroup。child/son/daughter 使用 child；parent/mother/father 使用 adult；grandparent/grandmother/grandfather 使用 elder。父母关系优先于白发、皱纹、服装或坐姿，不能仅凭显老外观使用 elder；只有明确爷爷奶奶辈、祖父母或老年身份才使用 elder。小孩使用 girl-baby/boy-baby，青年、成年人、父母、夫妻使用 girl/boy，明确老年人使用 girl-old/boy-old。年龄不明的主角必须使用 role=girl、ageGroup=unknown；多人场景中年龄不明但性别明确的角色使用对应青年 role；孩子性别不明使用 girl-baby。characters 缺失时按 character 生成单角色列表；普通非男女组合强制只保留一个女性角色。sceneCharacterCount 缺失时按 1，allowIntentionalDuplicate 默认 false；只有来源文字或画面明确表达双胞胎、分身或镜像时才允许 true。每张角色参考图只对应一个人物，次要人物必须与主角及彼此拥有明显不同的脸型、发型、发色、体型、年龄、服装、配饰或姿态；brand off 时也执行这条规则。不要伪造来源内容。
来源正文非空白字符数：{source_count}
来源内容：
{source_text}
来源 URL：{source.get('sourceUrl', '')}
""".strip()
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        if image_mode != "comic":
            for block in blocks:
                raw = block.get("localPath")
                if raw:
                    path = (work_dir / raw).resolve()
                    if path.is_file():
                        content.append({"type": "input_image", "image_url": _image_data_url(path), "detail": "high"})
        if brand == "on":
            for character, filename in CHARACTER_REFERENCES.items():
                reference = PHOTOS / filename
                if reference.is_file():
                    content.append({"type": "input_image", "image_url": _image_data_url(reference), "detail": "high"})
        progress("正在进行文章和图片语义分析")
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(_endpoint("responses"), headers={**_headers(), "Content-Type": "application/json"}, json={"model": self.text_model, "input": [{"role": "user", "content": content}]})
                response.raise_for_status()
                response_text = _response_text(response.json())
                try:
                    return _extract_json(response_text)
                except PipelineError:
                    (work_dir / "planner-response.txt").write_text(response_text, encoding="utf-8")
                    raise
        except httpx.HTTPError as exc:
            raise PipelineError(f"文本/视觉 API 调用失败：{exc}") from exc

    def generate(
        self,
        prompt: str,
        references: list[Path],
        output: Path,
        request_dir: Path | None = None,
        attempt: int = 1,
    ) -> None:
        image_api_mode = _env("IMAGE_API_MODE", "edits").lower()
        if references and image_api_mode == "generations":
            raise PipelineError("当前 Web-v3 的 IMAGE_API_MODE=generations 不支持参考图，请改为 edits。")
        endpoint = _endpoint("images/edits" if references else "images/generations")
        debug_dir = _api_debug_dir(output, request_dir)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if references:
                    field = _env("IMAGE_EDIT_FIELD", "image[]")
                    fields = {
                        "model": self.image_model,
                        "prompt": prompt,
                        "size": "1024x1536",
                        "quality": "medium",
                    }
                    file_specs = [
                        {
                            "field": field,
                            "path": str(ref.resolve()),
                            "filename": ref.name,
                            "content_type": mimetypes.guess_type(ref.name)[0] or "application/octet-stream",
                        }
                        for ref in references
                    ]
                    artifacts = _save_image_api_request(
                        output, endpoint, self.image_model, prompt, fields, file_specs,
                        request_dir=debug_dir,
                        attempt=attempt,
                    )
                    files = [
                        (spec["field"], (spec["filename"], Path(spec["path"]).read_bytes(), spec["content_type"]))
                        for spec in file_specs
                    ]
                    response = client.post(endpoint, headers=_headers(), data=fields, files=files)
                else:
                    payload = {"model": self.image_model, "prompt": prompt, "size": "1024x1536", "quality": "medium"}
                    artifacts = _save_image_api_request(
                        output, endpoint, self.image_model, prompt, {}, [],
                        request_dir=debug_dir, json_body=payload, attempt=attempt,
                    )
                    response = client.post(
                        endpoint,
                        headers={**_headers(), "Content-Type": "application/json"},
                        json=payload,
                    )
                _record_image_api_response(artifacts["request"], artifacts["response"], response)
                response.raise_for_status()
                _save_image_response(response.json(), output, client)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            detail = _response_preview(response)
            raise PipelineError(
                f"图片生成 API 调用失败：HTTP {response.status_code}；响应 body：{detail}；"
                f"请求备份：{artifacts['postman']}；响应日志：{artifacts['response']}"
            ) from exc
        except httpx.HTTPError as exc:
            suffix = f"；请求备份：{artifacts['postman']}" if "artifacts" in locals() else ""
            raise PipelineError(f"图片生成 API 调用失败：{exc}{suffix}") from exc


def _save_image_response(payload: dict, output: Path, client: httpx.Client) -> None:
    data = payload.get("data") or []
    if not data or not isinstance(data[0], dict):
        raise PipelineError("图片 API 没有返回图片数据。")
    item = data[0]
    if item.get("b64_json"):
        output.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        downloaded = client.get(item["url"])
        downloaded.raise_for_status()
        output.write_bytes(downloaded.content)
    else:
        raise PipelineError("图片 API 返回中缺少 b64_json 或 url。")


def _run_command(args: list[str], cwd: Path) -> dict | None:
    completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise PipelineError(detail or f"命令失败：{' '.join(args)}")
    output = completed.stdout.strip()
    try:
        value = json.loads(output)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for start, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _normalize_character(value: object) -> str:
    character = str(value or "").strip().lower()
    return character if character in CHARACTER_REFERENCES else DEFAULT_CHARACTER


def _normalize_scene_character_count(value: object) -> int:
    if isinstance(value, bool):
        return DEFAULT_SCENE_CHARACTER_COUNT
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCENE_CHARACTER_COUNT
    return count if count > 0 else DEFAULT_SCENE_CHARACTER_COUNT


def _normalize_scene_type(value: object) -> str:
    scene_type = str(value or "").strip().lower()
    return scene_type if scene_type in SCENE_TYPES else "single"


def _normalize_age_group(value: object) -> str:
    age_group = str(value or "").strip().lower()
    return age_group if age_group in AGE_GROUPS else "unknown"


def _infer_age_group_from_role(role: object) -> str:
    normalized = _normalize_character(role)
    if normalized.endswith("-baby"):
        return "child"
    if normalized.endswith("-old"):
        return "elder"
    return "adult"


def _purpose_age_group(purpose: object) -> str | None:
    value = str(purpose or "").strip().lower().replace("_", "-")
    if value in {"child", "son", "daughter"}:
        return "child"
    if value in {"grandparent", "grandmother", "grandfather", "grandma", "grandpa"}:
        return "elder"
    if value in {"parent", "mother", "father"}:
        return "adult"
    return None


def _role_gender(role: object) -> str:
    normalized = _normalize_character(role)
    return "male" if normalized in MALE_BY_AGE else "female"


def _role_for_gender_age(gender: str, age_group: str) -> str:
    if age_group == "child":
        return "boy-baby" if gender == "male" else "girl-baby"
    if age_group == "elder":
        return "boy-old" if gender == "male" else "girl-old"
    return "boy" if gender == "male" else "girl"


def _female_role(role: object) -> str:
    normalized = _normalize_character(role)
    return FEMALE_EQUIVALENTS.get(normalized, DEFAULT_CHARACTER)


def _male_role(role: object) -> str:
    normalized = _normalize_character(role)
    return MALE_EQUIVALENTS.get(normalized, "boy")


def _raw_character_roles(block: dict) -> list[dict]:
    raw = block.get("characters")
    roles: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                role = _normalize_character(item.get("role"))
                purpose = str(item.get("purpose") or "support").strip() or "support"
                raw_age_group = item.get("ageGroup")
                age_group = (
                    _normalize_age_group(raw_age_group)
                    if raw_age_group is not None
                    else _infer_age_group_from_role(role)
                )
                age_evidence = str(item.get("ageEvidence") or "").strip()
                roles.append({
                    "role": role,
                    "purpose": purpose,
                    "ageGroup": age_group,
                    "ageEvidence": age_evidence,
                })
            elif isinstance(item, str):
                role = _normalize_character(item)
                roles.append({
                    "role": role,
                    "purpose": "support",
                    "ageGroup": _infer_age_group_from_role(role),
                    "ageEvidence": "",
                })
    if not roles:
        role = _normalize_character(block.get("character"))
        raw_age_group = block.get("ageGroup")
        roles.append({
            "role": role,
            "purpose": "main",
            "ageGroup": (
                _normalize_age_group(raw_age_group)
                if raw_age_group is not None
                else _infer_age_group_from_role(role)
            ),
            "ageEvidence": "",
        })
    main_index = next((index for index, item in enumerate(roles) if item["purpose"] == "main"), None)
    if main_index is None:
        roles[0]["purpose"] = "main"
    elif main_index:
        main = roles.pop(main_index)
        roles.insert(0, main)
    return roles


def _dedupe_roles(roles: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for item in roles:
        role = _normalize_character(item.get("role"))
        if role in seen:
            continue
        seen.add(role)
        result.append({
            "role": role,
            "purpose": str(item.get("purpose") or "support").strip() or "support",
            "ageGroup": _normalize_age_group(item.get("ageGroup")),
            "ageEvidence": str(item.get("ageEvidence") or "").strip(),
        })
    return result


def _effective_age_group(item: dict) -> str:
    relation_group = _purpose_age_group(item.get("purpose"))
    if relation_group:
        return relation_group
    return _normalize_age_group(item.get("ageGroup"))


def _normalized_role_entry(item: dict, default_age_group: str) -> dict:
    age_group = _effective_age_group(item)
    if age_group == "unknown":
        age_group = default_age_group
    return {
        "role": _role_for_gender_age(_role_gender(item.get("role")), age_group),
        "purpose": str(item.get("purpose") or "support").strip() or "support",
        "ageGroup": age_group,
        "ageEvidence": str(item.get("ageEvidence") or "").strip(),
    }


def _normalize_character_roles(block: dict, scene_type: str) -> list[dict]:
    roles = _dedupe_roles(_raw_character_roles(block))
    main_entry = roles[0] if roles else {
        "role": DEFAULT_CHARACTER,
        "purpose": "main",
        "ageGroup": "unknown",
        "ageEvidence": "",
    }
    if scene_type in {"mixed_pair", "romantic_pair", "family_trio"}:
        if scene_type == "family_trio":
            child_entries = [item for item in roles if _effective_age_group(item) == "child"]
            adult_entries = [item for item in roles if _effective_age_group(item) != "child"]
            default_age_group = next(
                (
                    _effective_age_group(item)
                    for item in adult_entries
                    if _effective_age_group(item) != "unknown"
                ),
                "adult",
            )
        else:
            child_entries = []
            adult_entries = roles
            main_age_group = _effective_age_group(main_entry)
            default_age_group = main_age_group if main_age_group != "unknown" else "adult"

        male_entry = next((item for item in adult_entries if _role_gender(item["role"]) == "male"), None)
        female_entry = next((item for item in adult_entries if _role_gender(item["role"]) == "female"), None)
        main = _normalized_role_entry(main_entry, default_age_group)
        if male_entry is None:
            male_entry = {"role": "boy", "purpose": "partner", "ageGroup": default_age_group, "ageEvidence": ""}
        if female_entry is None:
            female_entry = {"role": "girl", "purpose": "partner", "ageGroup": default_age_group, "ageEvidence": ""}
        pair = [main]
        for entry in (male_entry, female_entry):
            normalized = _normalized_role_entry(entry, default_age_group)
            if not any(item["role"] == normalized["role"] for item in pair):
                pair.append(normalized)
        roles = pair
        if scene_type == "family_trio":
            child_entry = child_entries[0] if child_entries else {
                "role": "girl-baby",
                "purpose": "child",
                "ageGroup": "child",
                "ageEvidence": "孩子性别不明，使用 girl-baby",
            }
            child = _normalized_role_entry(child_entry, "child")
            child["ageGroup"] = "child"
            if not any(item["role"] == child["role"] for item in roles):
                roles.append(child)
    else:
        # Ordinary scenes keep one female visual anchor. Explicitly unknown
        # age is intentionally conservative and never becomes an old role.
        main_age_group = _effective_age_group(main_entry)
        if main_age_group == "unknown":
            main_age_group = "unknown"
        if main_age_group == "unknown":
            role = DEFAULT_CHARACTER
        else:
            role = _role_for_gender_age("female", main_age_group)
        roles = [{
            "role": role,
            "purpose": "main",
            "ageGroup": main_age_group,
            "ageEvidence": str(main_entry.get("ageEvidence") or "").strip(),
        }]
    return roles


def _reference_role_order(roles: list[dict]) -> list[str]:
    """Use male, female, then child order for stable multipart references."""
    values = [item["role"] for item in roles]
    ordered: list[str] = []
    for role in values:
        if role in MALE_BY_AGE and not role.endswith("-baby") and role not in ordered:
            ordered.append(role)
    for role in values:
        if role in FEMALE_BY_AGE and not role.endswith("-baby") and role not in ordered:
            ordered.append(role)
    for role in values:
        if role.endswith("-baby") and role not in ordered:
            ordered.append(role)
    for role in values:
        if role not in ordered:
            ordered.append(role)
    return ordered


def _normalize_scene_metadata(block: dict, position: int, warnings: list[str]) -> dict:
    """Normalize scene type, role references, and multi-person metadata."""
    raw_scene_type = block.get("sceneType")
    scene_type = _normalize_scene_type(raw_scene_type)
    if raw_scene_type is not None and str(raw_scene_type).strip().lower() not in SCENE_TYPES:
        warnings.append(
            f"article-plan.json.blocks[{position}].sceneType 无效，已回退为 single。"
        )
    raw_count = block.get("sceneCharacterCount")
    count = _normalize_scene_character_count(raw_count)
    if raw_count is not None and count == DEFAULT_SCENE_CHARACTER_COUNT and raw_count != 1:
        warnings.append(
            f"article-plan.json.blocks[{position}].sceneCharacterCount 无效，已回退为 1。"
        )
    notes = block.get("secondaryCharacterNotes")
    if not isinstance(notes, str):
        notes = ""
    allow_duplicate = block.get("allowIntentionalDuplicate", False)
    if not isinstance(allow_duplicate, bool):
        warnings.append(
            f"article-plan.json.blocks[{position}].allowIntentionalDuplicate 无效，已按 false 处理。"
        )
        allow_duplicate = False
    roles = _normalize_character_roles(block, scene_type)
    block["sceneCharacterCount"] = count
    block["secondaryCharacterNotes"] = notes.strip()
    block["allowIntentionalDuplicate"] = allow_duplicate
    block["sceneType"] = scene_type
    block["characters"] = roles
    block["character"] = roles[0]["role"]
    return block


def _character_reference(character: object) -> Path:
    return PHOTOS / CHARACTER_REFERENCES[_normalize_character(character)]


def _reference_paths(image_mode: str, brand: str, source_block: dict, characters: object, work_dir: Path) -> list[Path]:
    references: list[Path] = []
    if image_mode == "copy" and source_block.get("localPath"):
        references.append((work_dir / source_block["localPath"]).resolve())
    if brand == "on":
        if isinstance(characters, list):
            roles = _dedupe_roles([item if isinstance(item, dict) else {"role": item} for item in characters])
        else:
            roles = [{"role": _normalize_character(characters), "purpose": "main"}]
        for role in _reference_role_order(roles):
            references.append(_character_reference(role))
    return [reference for reference in references if reference.is_file()]


def _reference_instruction(image_mode: str, brand: str, comic: bool = False) -> str:
    if comic:
        return "只使用当前叙事节点选择的角色参考图；每张参考图只对应一个角色，不得把同一参考图复制给其他人物；不得参考来源图片。" if brand == "on" else "不使用品牌人物参考图；仍需让同一画面中的多个角色拥有明显不同的外观。"
    if brand != "on":
        return "只依据允许提供的来源图和文字完成画面，不添加品牌人物参考；同一画面中的多个角色必须拥有明显不同的外观。"
    if image_mode == "copy":
        return "来源图片负责信息结构、动作、道具、人物数量、空间关系和排版气质；普通单主角时角色参考图只用于主角，组合场景按 prompt 中的角色映射分别使用，不复制水印、署名或像素布局。"
    return "角色参考图按 prompt 中的角色映射分别使用；根据语义自由重构场景，不复刻来源构图。"


def _scene_character_instruction(block: dict) -> str:
    count = _normalize_scene_character_count(block.get("sceneCharacterCount"))
    notes = str(block.get("secondaryCharacterNotes") or "").strip()
    allow_duplicate = block.get("allowIntentionalDuplicate") is True
    scene_type = _normalize_scene_type(block.get("sceneType"))
    roles = _normalize_character_roles(block, scene_type)
    role_map = "、".join(
        f"{item['role']}/{item.get('ageGroup', 'unknown')}/{item.get('purpose', 'support')}"
        for item in roles
    )
    if count <= 1:
        return f"画面只安排 1 个主要人物，使用女性年龄角色 {role_map or 'girl'}，不要额外添加第二个人物。"
    if allow_duplicate:
        return (
            f"画面安排 {count} 个人物；当前计划明确允许双胞胎、分身或镜像式重复外貌，"
            "仅在语义需要的位置保留重复，其余人物仍应有清晰区分。"
        )
    detail = f"次要人物差异要求：{notes}。" if notes else "请自行设计清晰的差异。"
    return (
        f"画面安排 {count} 个人物，场景类型为 {scene_type}，角色计划为 {role_map or 'girl'}。"
        f"每张角色参考图只对应计划中的一个人物；次要人物不得复制主角或彼此的脸型、发型、"
        f"发色、体型、年龄、服装、配饰和姿态，必须明显不同。{detail}"
        "除非明确是双胞胎、分身或镜像，否则禁止出现两个外貌近乎相同的人物。"
    )


def _character_reference_instruction(block: dict) -> str:
    roles = _reference_role_order(_normalize_character_roles(block, _normalize_scene_type(block.get("sceneType"))))
    if not roles:
        return "角色参考图映射：参考图 1 = girl。"
    role_lookup = {
        item["role"]: item
        for item in _normalize_character_roles(block, _normalize_scene_type(block.get("sceneType")))
    }
    mapping = "；".join(
        f"参考图 {index} = {role} / {role_lookup.get(role, {}).get('ageGroup', 'unknown')} / {role_lookup.get(role, {}).get('purpose', 'support')}"
        for index, role in enumerate(roles, 1)
    )
    return f"角色参考图映射（不含 copy 模式最前面的来源图）：{mapping}。每张参考图只用于对应角色。"


def _build_image_prompt(block: dict, image_mode: str, brand: str, caption_mode: str) -> str:
    prompt = str(block.get("imagePrompt") or "根据文章语义生成纵向叙事卡片")
    prompt += "\n" + _reference_instruction(image_mode, brand, image_mode == "comic")
    if brand == "on":
        prompt += "\n" + _character_reference_instruction(block)
    prompt += "\n" + _scene_character_instruction(block)
    if brand == "on":
        main_role = _normalize_character_roles(block, _normalize_scene_type(block.get("sceneType")))[0]["role"]
        prompt += f"\n当前主角角色参考：{main_role}；普通非男女组合使用女性年龄角色，男女组合按角色映射分别使用。"
    if image_mode == "comic":
        prompt += "\n生成完整 1122×1402 竖向叙事卡片，包含少量中文标题、旁白或对白；只生成当前连续叙事节点。"
    elif caption_mode == "ai":
        prompt += f"\n生成完整纵向卡片，使用 #F7F6F6 淡灰白背景，并准确包含中文文案：{block.get('caption', '')}。"
    else:
        prompt += "\n只生成无文字的上半部分插画，不要生成汉字、字母、水印或 Logo。"
    return prompt


def _is_eof_failure(error: BaseException) -> bool:
    """Recognize EOF failures from either httpx or CPA's wrapped HTTP body."""
    messages: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    text = " ".join(messages).lower()
    return bool(
        re.search(r"\b(unexpected\s+eof|eof)\b", text)
        or "end of file" in text
        or "peer closed" in text
        or "server disconnected" in text
        or "connection closed" in text
        or "connection reset" in text
    )


def _generate_image_with_retry(
    ai: OpenAICompatible,
    prompt: str,
    references: list[Path],
    output: Path,
    request_dir: Path,
    image_number: int,
    schedule_anchor: float,
    progress: Progress,
) -> None:
    """Submit one image at its scheduled time and retry one EOF once."""
    scheduled_at = schedule_anchor + (image_number - 1) * IMAGE_START_INTERVAL_SECONDS
    remaining = scheduled_at - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)

    first_error: Exception | None = None
    for attempt in range(1, IMAGE_EOF_RETRY_COUNT + 2):
        try:
            ai.generate(prompt, references, output, request_dir=request_dir, attempt=attempt)
            return
        except Exception as exc:
            if attempt > IMAGE_EOF_RETRY_COUNT or not _is_eof_failure(exc):
                if first_error is not None:
                    raise PipelineError(
                        f"图片 {image_number} 首次请求遇到 EOF，已重试 {IMAGE_EOF_RETRY_COUNT} 次仍失败；"
                        f"首次错误：{first_error}；最终错误：{exc}"
                    ) from exc
                raise
            first_error = exc
            progress(f"图片 {image_number} 遇到 EOF，{IMAGE_EOF_RETRY_DELAY_SECONDS:g} 秒后重试")
            time.sleep(IMAGE_EOF_RETRY_DELAY_SECONDS)


def _repair_missing_source_image_links(
    article_blocks: list[dict],
    source_blocks: list[dict],
    source_images: dict[int, dict],
    missing_indexes: set[int],
) -> list[dict]:
    """Preserve omitted source images as links while keeping article order useful."""
    if not missing_indexes:
        return article_blocks

    text_rank_by_image: dict[int, int] = {}
    text_rank = 0
    for source_block in sorted(source_blocks, key=lambda item: item.get("sourceIndex", -1)):
        if source_block.get("type") == "image":
            source_index = source_block.get("sourceIndex")
            if isinstance(source_index, int):
                text_rank_by_image[source_index] = text_rank
        elif source_block.get("type") == "text":
            text_rank += 1

    source_image_order = sorted(source_images)
    image_number = {source_index: position for position, source_index in enumerate(source_image_order, 1)}
    insertions: dict[int, list[dict]] = {}
    for source_index in sorted(missing_indexes):
        source_block = source_images[source_index]
        link: dict = {
            "type": "sourceImageLink",
            "sourceIndex": source_index,
            "alt": source_block.get("alt") or f"原图 {image_number.get(source_index, source_index)}",
            "error": "模型未返回该来源图片的生成计划，已保留原始图片链接",
        }
        url = source_block.get("url")
        if isinstance(url, str) and url:
            link["url"] = url
        rank = text_rank_by_image.get(source_index, 0)
        insertions.setdefault(rank, []).append(link)

    repaired: list[dict] = []
    text_rank = 0
    for block in article_blocks:
        repaired.append(block)
        if block.get("type") == "text":
            text_rank += 1
            repaired.extend(insertions.pop(text_rank, []))
    for rank in sorted(insertions):
        repaired.extend(insertions[rank])
    return repaired


def _promote_source_image_links_to_jobs(
    article_blocks: list[dict],
    source_images: dict[int, dict],
    removed_indexes: set[int],
) -> list[dict]:
    """Turn planner/fallback link placeholders back into image jobs.

    The planner must return image jobs. Keeping this normalization here makes
    the pipeline tolerant of older or non-compliant model responses, while
    sourceImageLink remains reserved for the post-generation failure path.
    """
    for block in article_blocks:
        if block.get("type") != "sourceImageLink":
            continue
        source_index = block.get("sourceIndex")
        if not isinstance(source_index, int) or source_index not in source_images or source_index in removed_indexes:
            continue
        block["type"] = "image"
        block.pop("error", None)
        if not str(block.get("imagePrompt") or "").strip():
            alt = str(block.get("alt") or "").strip()
            block["imagePrompt"] = (
                "根据来源图片的信息结构、动作、道具和空间关系，结合文章语义重新生成纵向插画；"
                + (f"画面主题：{alt}。" if alt else "不要添加署名、水印、二维码或多余文字。")
            )
    return article_blocks


def run_pipeline(task_id: str, url: str, article_mode: str, image_mode: str, comic_count: int, caption_mode: str, progress: Progress, brand: str = "on") -> dict:
    work_dir = BLOG_ROOT / ".recreate-work" / task_id
    work_dir.mkdir(parents=True, exist_ok=False)
    progress("正在直接获取微信文章页面")
    fetch_args = [sys.executable, str(SCRIPTS / "fetch_article.py"), url, "--work-dir", str(work_dir), "--config", str(WEB_ROOT / "config.json")]
    if image_mode != "comic":
        fetch_args.insert(-2, "--download-images")
    fetch = _run_command(fetch_args, BLOG_ROOT)
    source_path = work_dir / "source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    plan = OpenAICompatible().plan(source, work_dir, article_mode, image_mode, comic_count, caption_mode, progress, brand)
    plan = _normalize_article_plan(source, plan, article_mode, image_mode, progress)
    if not plan.get("title"):
        raise PipelineError("模型返回的文章计划缺少标题，且原文标题不可用。")
    if image_mode == "comic":
        plan["comicCount"] = comic_count
    article_blocks = [block for block in plan["blocks"] if isinstance(block, dict)]
    source_blocks = [block for block in source.get("blocks", []) if isinstance(block, dict)]
    source_images = {b.get("sourceIndex"): b for b in source_blocks if b.get("type") == "image"}
    raw_dir, cards_dir = work_dir / "raw-cards", work_dir / "cards"
    raw_dir.mkdir(); cards_dir.mkdir()
    if image_mode == "comic":
        jobs = [(block, None, raw_dir / f"image-{n:03d}.png", cards_dir / f"image-{n:03d}.png") for n, block in enumerate(article_blocks, 1) if block.get("type") == "image"]
        if len(jobs) != comic_count:
            raise PipelineError(f"模型返回 {len(jobs)} 张 comic 图片，期望 {comic_count} 张。")
    else:
        removed = {
            item for item in plan.get("removedSourceIndexes", [])
            if isinstance(item, int) and not isinstance(item, bool)
        }
        article_blocks = _promote_source_image_links_to_jobs(article_blocks, source_images, removed)
        planned_images = [
            block for block in article_blocks
            if block.get("type") in {"image", "sourceImageLink"}
        ]
        expected = set(source_images) - removed
        planned_indexes = [block.get("sourceIndex") for block in planned_images]
        actual = {source_index for source_index in planned_indexes if isinstance(source_index, int)}
        duplicates = len(planned_indexes) != len(actual)
        unexpected = actual - expected
        missing = expected - actual
        if duplicates or unexpected:
            raise PipelineError(
                f"模型文章计划的来源图片不完整或重复：期望={sorted(expected)}，实际={sorted(actual)}"
            )
        if missing:
            article_blocks = _repair_missing_source_image_links(
                article_blocks, source_blocks, source_images, missing
            )
            article_blocks = _promote_source_image_links_to_jobs(article_blocks, source_images, removed)
            plan["blocks"] = article_blocks
        requested = [block for block in article_blocks if block.get("type") == "image"]
        if not article_blocks and not expected:
            raise PipelineError("文章计划没有任何可用正文或图片内容。")
        if expected and not requested:
            raise PipelineError(
                f"期望为 {len(expected)} 张来源图片创建生图任务，但实际没有可执行的图片任务。"
            )
        jobs = [(block, source_images[block["sourceIndex"]], raw_dir / f"image-{n:03d}.png", cards_dir / f"image-{n:03d}.png") for n, block in enumerate(requested, 1)]
    progress(f"正在生成 {len(jobs)} 张配图")
    ai = OpenAICompatible()

    def generate_one(job_index: int, job, schedule_anchor: float):
        block, source_block, raw, final = job
        character = _normalize_character(block.get("character"))
        characters = block.get("characters") or [{"role": character, "purpose": "main"}]
        refs = _reference_paths(image_mode, brand, source_block or {}, characters, work_dir)
        prompt = _build_image_prompt(block, image_mode, brand, caption_mode)
        try:
            _generate_image_with_retry(
                ai, prompt, refs, raw, work_dir / "api-debug", job_index + 1, schedule_anchor, progress
            )
            return block, raw, final, None
        except Exception as exc:
            return block, raw, final, exc

    schedule_anchor = time.monotonic()
    indexed_jobs = list(enumerate(jobs))
    if indexed_jobs:
        with ThreadPoolExecutor(max_workers=len(indexed_jobs)) as pool:
            generated = list(pool.map(lambda item: generate_one(item[0], item[1], schedule_anchor), indexed_jobs))
    else:
        generated = []

    def finalize_one(item):
        block, raw, final, error = item
        if error:
            block["type"] = "generatedImageLink" if image_mode == "comic" else "sourceImageLink"
            block["error"] = str(error)
            for key in ("imagePrompt", "caption", "emotion"):
                block.pop(key, None)
            return block, error
        try:
            if image_mode == "comic" or caption_mode == "ai":
                _run_command([sys.executable, str(SCRIPTS / "normalize_ai_card.py"), "--input", str(raw), "--output", str(final)], BLOG_ROOT)
            else:
                args = [sys.executable, str(SCRIPTS / "compose_card.py"), "--illustration", str(raw), "--caption", str(block.get("caption") or ""), "--emotion", str(block.get("emotion") or "通用"), "--output", str(final)]
                if brand == "on":
                    pos = args.index("--caption")
                    main_role = _raw_character_roles(block)[0]["role"]
                    args[pos:pos] = ["--reference", str(_character_reference(main_role))]
                _run_command(args, BLOG_ROOT)
            block["path"] = str(final)
            for key in ("imagePrompt", "caption", "emotion"):
                block.pop(key, None)
            return block, None
        except Exception as exc:
            block["type"] = "generatedImageLink" if image_mode == "comic" else "sourceImageLink"
            block["error"] = str(exc)
            for key in ("imagePrompt", "caption", "emotion"):
                block.pop(key, None)
            return block, exc

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(generated)))) as pool:
        finalized = list(pool.map(finalize_one, generated))
    errors = [error for _, error in finalized if error]
    image_failure_log = _write_image_failure_log(work_dir, generated, finalized)
    article_path = work_dir / "article-plan.json"
    article_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    progress("正在构建和校验离线 HTML")
    manifest_path = work_dir / "manifest.json"
    manifest_result = _run_command([sys.executable, str(SCRIPTS / "build_manifest.py"), "--source", str(source_path), "--article", str(article_path), "--output", str(manifest_path)], BLOG_ROOT)
    for warning in (manifest_result or {}).get("warnings", []):
        if isinstance(warning, str) and warning.strip():
            progress(f"提示：{warning.strip()}")
    try:
        rendered = _run_command([sys.executable, str(SCRIPTS / "render_article.py"), "--manifest", str(manifest_path), "--result-dir", str(BLOG_ROOT / "result")], BLOG_ROOT)
    except Exception as exc:
        if image_failure_log:
            raise PipelineError(f"{exc}；图片失败明细：{image_failure_log.resolve()}") from exc
        raise
    if not rendered or not rendered.get("packageDir") or not rendered.get("html"):
        raise PipelineError("渲染完成但没有返回 result 输出目录。")
    package, html_file = Path(rendered["packageDir"]), Path(rendered["html"])
    expected_size = "1122x1402" if image_mode == "comic" or caption_mode == "ai" else "1080x1440"
    _run_command([sys.executable, str(SCRIPTS / "validate_output.py"), str(html_file), "--expected-image-size", expected_size], BLOG_ROOT)
    return {
        "title": plan["title"],
        "result_dir": str(package.resolve()),
        "html_file": str(html_file.resolve()),
        "image_failures": len(errors),
        "image_failure_log": str(image_failure_log.resolve()) if image_failure_log else None,
        "fetch_summary": fetch,
    }
