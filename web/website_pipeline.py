from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx


Progress = Callable[[str], None]


class PipelineError(RuntimeError):
    pass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


BLOG_ROOT = Path(_env("BLOG_ROOT", str(Path(__file__).resolve().parents[1]))).resolve()
SKILL_ROOT = BLOG_ROOT / ".agents" / "skills" / "recreate-wechat-article"
SCRIPTS = SKILL_ROOT / "scripts"
PHOTOS = BLOG_ROOT / "photos"


def _base_url() -> str:
    return _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def _endpoint(path: str) -> str:
    return f"{_base_url()}/{path.lstrip('/')}"


def _headers() -> dict[str, str]:
    key = _env("OPENAI_API_KEY", "")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"} if key else {"Accept": "application/json"}


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise PipelineError("模型没有返回有效的 JSON 文章计划。")
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PipelineError(f"模型返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("模型返回的文章计划必须是对象。")
    return value


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


class OpenAICompatible:
    def __init__(self):
        self.text_model = _env("TEXT_MODEL", "gpt-5.6-luna")
        self.image_model = _env("IMAGE_MODEL", "gpt-image-2")
        self.timeout = float(_env("AI_TIMEOUT_SECONDS", "900"))

    def plan(self, source: dict, work_dir: Path, image_mode: str, caption_mode: str, progress: Progress, brand: str = "on") -> dict:
        if not _env("OPENAI_API_KEY", ""):
            raise PipelineError("Web runner 未配置 OPENAI_API_KEY，请填写 web/.env。")
        blocks = source.get("blocks", [])
        source_text = "\n".join(
            f"[{block.get('sourceIndex')}] {block.get('type')}: {block.get('text', block.get('alt', ''))}"
            for block in blocks if isinstance(block, dict)
        )
        brand_context = " 使用项目角色参考图来保持人物身份与视觉风格一致。" if brand == "on" else ""
        prompt = f"""
你是 recreate-wechat-article 项目的文章重构规划器。请严格根据输入文章内容和图片完成原创重构。
图片模式：{image_mode}；文案模式：{caption_mode}。{brand_context}
只返回 JSON，不要 Markdown，不要解释。JSON 必须符合：
{{
  "title": "原创标题",
  "summary": "可选原创导语",
  "removedSourceIndexes": [0],
  "blocks": [
    {{"type":"heading","level":2,"text":"新章节"}},
    {{"type":"text","text":"原创段落。"}},
    {{"type":"image","sourceIndex":1,"emotion":"通用","caption":"图片文案","imagePrompt":"详细生图提示词","alt":"图片说明"}}
  ]
}}
要求：保留的每个来源图片必须在 blocks 中恰好出现一次，按 sourceIndex 递增；图片必须包含 emotion、caption、imagePrompt、alt；文章中文原创，正文总量最多 800 个非空白字符；不得复制原文句子；删除二维码、广告、作者信息和推广内容。
来源文章块：
{source_text}
来源 URL：{source.get('sourceUrl', '')}
""".strip()
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        for block in blocks:
            raw = block.get("localPath") if isinstance(block, dict) else None
            if not raw:
                continue
            path = (work_dir / raw).resolve()
            if path.is_file():
                content.append({"type": "input_image", "image_url": _image_data_url(path), "detail": "high"})
        if brand == "on":
            style = PHOTOS / "通用1.png"
            if style.is_file():
                content.append({"type": "input_image", "image_url": _image_data_url(style), "detail": "high"})
        progress("正在进行文章和图片语义分析")
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    _endpoint("responses"), headers={**_headers(), "Content-Type": "application/json"},
                    json={"model": self.text_model, "input": [{"role": "user", "content": content}]},
                )
                response.raise_for_status()
                return _extract_json(_response_text(response.json()))
        except httpx.HTTPError as exc:
            raise PipelineError(f"文本/视觉 API 调用失败：{exc}") from exc

    def generate(self, prompt: str, references: list[Path], output: Path) -> None:
        image_api_mode = _env("IMAGE_API_MODE", "edits").lower()
        if references and image_api_mode == "generations":
            raise PipelineError("当前 Web runner 的 IMAGE_API_MODE=generations 不支持参考图，请改为 edits。")
        if not references:
            self._generate_only(prompt, output)
            return
        files = []
        field = _env("IMAGE_EDIT_FIELD", "image[]")
        for reference in references:
            mime = mimetypes.guess_type(reference.name)[0] or "application/octet-stream"
            files.append((field, (reference.name, reference.read_bytes(), mime)))
        data = {"model": self.image_model, "prompt": prompt, "size": "1024x1536", "quality": "medium"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(_endpoint("images/edits"), headers=_headers(), data=data, files=files)
                response.raise_for_status()
                _save_image_response(response.json(), output, client)
        except httpx.HTTPError as exc:
            raise PipelineError(f"图片生成 API 调用失败：{exc}") from exc

    def _generate_only(self, prompt: str, output: Path) -> None:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    _endpoint("images/generations"),
                    headers={**_headers(), "Content-Type": "application/json"},
                    json={"model": self.image_model, "prompt": prompt, "size": "1024x1536", "quality": "medium"},
                )
                response.raise_for_status()
                _save_image_response(response.json(), output, client)
        except httpx.HTTPError as exc:
            raise PipelineError(f"图片生成 API 调用失败：{exc}") from exc


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


def _run_command(args: list[str], cwd: Path, progress: Progress) -> dict | None:
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
    # Existing skill scripts print indented JSON. Find the last complete JSON
    # object instead of trying to parse one line at a time.
    decoder = json.JSONDecoder()
    for start in (index for index, char in enumerate(output) if char == "{"):
        try:
            value, _ = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _emotion_reference(emotion: str) -> Path:
    aliases = {"喜": "喜1.png", "怒": "怒1.png", "哀": "哀1.png", "乐": "乐1.png", "通用": "通用1.png"}
    return PHOTOS / aliases.get(emotion, "通用1.png")


def _reference_paths(image_mode: str, brand: str, source_block: dict, emotion: str, work_dir: Path) -> list[Path]:
    references: list[Path] = []
    local = source_block.get("localPath")
    if image_mode == "copy" and local:
        references.append((work_dir / local).resolve())
    if brand == "on":
        references.append(_emotion_reference(emotion))
    return [reference for reference in references if reference.is_file()]


def _reference_instruction(image_mode: str, brand: str) -> str:
    if brand != "on":
        return "只依据允许提供的来源图和文字完成画面，不添加额外角色参考。"
    if image_mode == "copy":
        return (
            "参考图按顺序提供：第一张是来源图片，负责动作、关键道具、空间关系、信息结构和排版气质；"
            "最后一张是品牌人物参考，品牌人物的身份、外貌、发型、饰品和统一 3D 视觉风格拥有最高优先级。"
            "用品牌人物替换来源主体，不复制水印、署名、原文或像素级布局。"
        )
    return (
        "最后一张参考图是品牌人物参考；品牌人物的身份、外貌、发型、饰品和统一 3D 视觉风格拥有最高优先级。"
        "根据文章语义自由重构场景，不复刻来源图片构图。"
    )


def run_pipeline(task_id: str, url: str, image_mode: str, caption_mode: str, progress: Progress, brand: str = "on") -> dict:
    work_dir = BLOG_ROOT / ".recreate-work" / task_id
    work_dir.mkdir(parents=True, exist_ok=False)
    progress("正在通过 n8n 抓取微信文章")
    config = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
    config["webhookUrl"] = _env("N8N_WEBHOOK_URL", config.get("webhookUrl", ""))
    config_path = work_dir / "runner-config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    fetch = _run_command(
        [sys.executable, str(SCRIPTS / "fetch_article.py"), url, "--work-dir", str(work_dir), "--download-images", "--config", str(config_path)],
        BLOG_ROOT, progress,
    )
    source_path = work_dir / "source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    ai = OpenAICompatible()
    plan = ai.plan(source, work_dir, image_mode, caption_mode, progress, brand)
    article_blocks = plan.get("blocks")
    if not isinstance(article_blocks, list) or not plan.get("title"):
        raise PipelineError("模型返回的文章计划缺少 title 或 blocks。")
    source_images = {b.get("sourceIndex"): b for b in source.get("blocks", []) if b.get("type") == "image"}
    requested = [b for b in article_blocks if isinstance(b, dict) and b.get("type") == "image"]
    if {b.get("sourceIndex") for b in requested} != set(source_images):
        raise PipelineError("模型文章计划没有为每张保留来源图片提供唯一结果。")

    raw_dir = work_dir / "raw-cards"
    cards_dir = work_dir / "cards"
    raw_dir.mkdir()
    cards_dir.mkdir()
    image_jobs: list[tuple[dict, Path, Path]] = []
    for number, block in enumerate(requested, start=1):
        raw = raw_dir / f"image-{number:03d}.png"
        final = cards_dir / f"image-{number:03d}.png"
        image_jobs.append((block, raw, final))

    progress(f"正在生成 {len(image_jobs)} 张配图")
    def generate_one(job: tuple[dict, Path, Path]) -> tuple[dict, Path, Path, Exception | None]:
        block, raw, final = job
        source_block = source_images[block["sourceIndex"]]
        emotion = str(block.get("emotion") or "通用")
        refs = _reference_paths(image_mode, brand, source_block, emotion, work_dir)
        prompt = str(block.get("imagePrompt") or "根据文章语义重制一张纵向插画卡片")
        prompt += f"\n{_reference_instruction(image_mode, brand)}"
        if caption_mode == "ai":
            prompt += f"\n生成完整纵向卡片，并准确包含中文文案：{block.get('caption', '')}。"
        else:
            prompt += "\n只生成无文字的上半部分插画，不要生成任何文字。"
        try:
            ai.generate(prompt, [ref for ref in refs if ref.is_file()], raw)
            return block, raw, final, None
        except Exception as exc:  # Each image is independent; keep other images running.
            return block, raw, final, exc

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(image_jobs)))) as pool:
        generated = list(pool.map(generate_one, image_jobs))

    def finalize_one(item: tuple[dict, Path, Path, Exception | None]) -> tuple[dict, Exception | None]:
        block, raw, final, error = item
        if error:
            block["type"] = "sourceImageLink"
            block.pop("imagePrompt", None)
            block.pop("caption", None)
            block.pop("emotion", None)
            return block, error
        try:
            reference = _emotion_reference(str(block.get("emotion") or "通用")) if brand == "on" else None
            if caption_mode == "ai":
                _run_command([sys.executable, str(SCRIPTS / "normalize_ai_card.py"), "--input", str(raw), "--output", str(final)], BLOG_ROOT, progress)
            else:
                compose_args = [
                    sys.executable, str(SCRIPTS / "compose_card.py"), "--illustration", str(raw),
                    "--caption", str(block.get("caption") or ""), "--emotion", str(block.get("emotion") or "通用"), "--output", str(final),
                ]
                if reference is not None:
                    insert_at = compose_args.index("--caption")
                    compose_args[insert_at:insert_at] = ["--reference", str(reference)]
                _run_command(compose_args, BLOG_ROOT, progress)
            block["path"] = str(final)
            block.pop("imagePrompt", None)
            block.pop("caption", None)
            block.pop("emotion", None)
            return block, None
        except Exception as exc:
            block["type"] = "sourceImageLink"
            block.pop("imagePrompt", None)
            block.pop("caption", None)
            block.pop("emotion", None)
            return block, exc

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(generated)))) as pool:
        finalized = list(pool.map(finalize_one, generated))
    errors = [error for _, error in finalized if error]
    plan["blocks"] = [block for block in article_blocks]
    article_path = work_dir / "article-plan.json"
    article_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    progress("正在构建和校验离线 HTML")
    manifest_path = work_dir / "manifest.json"
    _run_command([sys.executable, str(SCRIPTS / "build_manifest.py"), "--source", str(source_path), "--article", str(article_path), "--output", str(manifest_path)], BLOG_ROOT, progress)
    rendered = _run_command([sys.executable, str(SCRIPTS / "render_article.py"), "--manifest", str(manifest_path), "--result-dir", str(BLOG_ROOT / "result")], BLOG_ROOT, progress)
    if not rendered or not rendered.get("packageDir") or not rendered.get("html"):
        raise PipelineError("渲染完成但没有返回 result 输出目录。")
    package = Path(rendered["packageDir"])
    html_file = Path(rendered["html"])
    _run_command([sys.executable, str(SCRIPTS / "validate_output.py"), str(html_file), "--expected-image-size", "1122x1402" if caption_mode == "ai" else "1080x1440"], BLOG_ROOT, progress)
    return {
        "title": plan["title"], "result_dir": str(package.resolve()), "html_file": str(html_file.resolve()),
        "image_failures": len(errors), "fetch_summary": fetch,
    }
