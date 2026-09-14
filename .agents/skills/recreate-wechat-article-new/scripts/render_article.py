#!/usr/bin/env python3
"""Render a rewritten article manifest into a non-overwriting offline HTML package."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
LINE_ENDINGS = set("，。！？；,.!;?")

try:
    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    # Windows Python installations may not ship the IANA tz database.
    SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class ManifestError(ValueError):
    pass


def non_whitespace_count(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def rewritten_character_count(data: dict) -> int:
    total = non_whitespace_count(str(data.get("summary") or ""))
    for block in data.get("blocks", []):
        if isinstance(block, dict) and block.get("type") in {"text", "heading", "quote"}:
            total += non_whitespace_count(str(block.get("text") or ""))
    return total


def rewrite_character_limit(source_character_count: int) -> int:
    if source_character_count == 0:
        # Image-only sources have no textual baseline. Keep the global safety
        # cap while allowing a concise rewrite based on the source imagery.
        return 800
    return min(800, math.ceil(source_character_count * 0.70))


def sanitize_filename(title: str) -> str:
    value = INVALID_FILENAME.sub("-", title)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    value = re.sub(r"-{2,}", "-", value)[:80].rstrip(" .-")
    if not value:
        value = "未命名文章"
    if value.upper() in RESERVED_WINDOWS:
        value = f"文章-{value}"
    return value


def validate_source_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com" or not parsed.path.startswith("/s/"):
        raise ManifestError("sourceUrl 必须是 https://mp.weixin.qq.com/s/... 链接。")


def resolve_image_path(raw_path: str, manifest_path: Path) -> Path:
    path = Path(raw_path)
    candidates = [path] if path.is_absolute() else [manifest_path.parent / path, Path.cwd() / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise ManifestError(f"图片不存在：{raw_path}")


def validate_source_archive(data: dict, manifest_path: Path) -> dict | None:
    archive = data.get("sourceArchive")
    if archive is None:
        return None
    if not isinstance(archive, dict):
        raise ManifestError("sourceArchive 必须是对象。")
    title = archive.get("title")
    blocks = archive.get("blocks")
    if not isinstance(title, str) or not title.strip():
        raise ManifestError("sourceArchive.title 不能为空。")
    if not isinstance(blocks, list) or not blocks:
        raise ManifestError("sourceArchive.blocks 必须是非空数组。")

    validated_blocks: list[dict] = []
    archived_count = 0
    previous_source_index = -1
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ManifestError(f"sourceArchive.blocks[{index}] 必须是对象。")
        source_index = block.get("sourceIndex")
        if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
            raise ManifestError(f"sourceArchive.blocks[{index}].sourceIndex 必须是非负整数。")
        if source_index <= previous_source_index:
            raise ManifestError("sourceArchive.blocks 必须按 sourceIndex 严格递增排列。")
        previous_source_index = source_index
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ManifestError(f"sourceArchive.blocks[{index}].text 不能为空。")
            archived_count += non_whitespace_count(text)
            validated_blocks.append(dict(block))
            continue
        if block_type != "image":
            raise ManifestError(f"sourceArchive.blocks[{index}].type 仅允许 text 或 image。")
        url = block.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or not parsed.netloc:
            raise ManifestError(f"sourceArchive.blocks[{index}].url 必须是 HTTPS 图片链接。")
        validated = dict(block)
        raw_path = block.get("path")
        validated["_resolvedPath"] = None
        if raw_path is not None:
            if not isinstance(raw_path, str) or raw_path.startswith(("http://", "https://")):
                raise ManifestError(f"sourceArchive.blocks[{index}].path 必须是本地图片路径。")
            try:
                validated["_resolvedPath"] = resolve_image_path(raw_path, manifest_path)
            except ManifestError:
                validated["_localWarning"] = f"原图 {source_index} 本地文件不存在：{raw_path}"
        validated_blocks.append(validated)

    source_character_count = data.get("sourceCharacterCount")
    if isinstance(source_character_count, bool) or not isinstance(source_character_count, int):
        raise ManifestError("提供 sourceArchive 时必须填写 sourceCharacterCount。")
    if archived_count != source_character_count:
        raise ManifestError(
            f"sourceCharacterCount 为 {source_character_count}，但 sourceArchive 正文实际为 {archived_count} 个非空白字符。"
        )
    return {"title": title.strip(), "blocks": validated_blocks}


def validate_manifest(data: dict, manifest_path: Path) -> tuple[list[Path], dict | None]:
    title = data.get("title")
    source_url = data.get("sourceUrl")
    blocks = data.get("blocks")
    if not isinstance(title, str) or not title.strip():
        raise ManifestError("title 不能为空。")
    if not isinstance(source_url, str):
        raise ManifestError("sourceUrl 缺失。")
    validate_source_url(source_url)
    if not isinstance(blocks, list) or not blocks:
        raise ManifestError("blocks 必须是非空数组。")
    article_mode = data.get("articleMode", "trans")
    image_mode = data.get("imageMode", "copy")
    if article_mode not in {"trans", "ori"}:
        raise ManifestError("articleMode 必须是 trans 或 ori。")
    if image_mode not in {"copy", "rebuild", "comic"}:
        raise ManifestError("imageMode 必须是 copy、rebuild 或 comic。")
    comic_count = data.get("comicCount", 5)
    if isinstance(comic_count, bool) or not isinstance(comic_count, int) or comic_count <= 0:
        raise ManifestError("comicCount 必须是正整数。")
    if image_mode == "comic" and article_mode != "ori":
        raise ManifestError("imageMode=comic 必须搭配 articleMode=ori。")
    source_character_count = data.get("sourceCharacterCount")
    if source_character_count is not None:
        if isinstance(source_character_count, bool) or not isinstance(source_character_count, int) or source_character_count < 0:
            raise ManifestError("sourceCharacterCount 必须是非负整数。")
        rewritten_count = rewritten_character_count(data)
        limit = rewrite_character_limit(source_character_count)
        if article_mode == "trans" and rewritten_count > limit:
            raise ManifestError(
                f"重写正文为 {rewritten_count} 个非空白字符，超过允许上限 {limit}；请继续浓缩。"
            )

    image_paths: list[Path] = []
    previous_source_image_index = -1
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ManifestError(f"blocks[{index}] 必须是对象。")
        block_type = block.get("type")
        if block_type in {"text", "heading", "quote"}:
            if not isinstance(block.get("text"), str) or not block["text"].strip():
                raise ManifestError(f"blocks[{index}].text 不能为空。")
            if block_type == "heading" and block.get("level", 2) not in (2, 3):
                raise ManifestError(f"blocks[{index}].level 仅允许 2 或 3。")
        elif block_type == "image":
            raw_path = block.get("path")
            if not isinstance(raw_path, str) or raw_path.startswith(("http://", "https://")):
                raise ManifestError(f"blocks[{index}].path 必须是本地重制图片。")
            image_paths.append(resolve_image_path(raw_path, manifest_path))
        elif block_type == "sourceImageLink":
            if image_mode == "comic":
                raise ManifestError("imageMode=comic 的失败结果必须使用 generatedImageLink。")
            source_index = block.get("sourceIndex")
            if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
                raise ManifestError(f"blocks[{index}].sourceIndex 必须是非负整数。")
            url = block.get("url")
            if url is not None:
                parsed = urlparse(url) if isinstance(url, str) else None
                if not parsed or parsed.scheme != "https" or not parsed.netloc:
                    raise ManifestError(f"blocks[{index}].url 必须是 HTTPS 图片链接或省略。")
        elif block_type == "generatedImageLink":
            if image_mode != "comic":
                raise ManifestError("generatedImageLink 仅允许用于 imageMode=comic。")
            if block.get("error") is not None and (
                not isinstance(block.get("error"), str) or not block["error"].strip()
            ):
                raise ManifestError(f"blocks[{index}].error 必须是非空字符串或省略。")
        else:
            raise ManifestError(f"blocks[{index}].type 不受支持：{block_type}")

        if block_type in {"image", "sourceImageLink"} and "sourceIndex" in block:
            current_source_index = block.get("sourceIndex")
            if isinstance(current_source_index, bool) or not isinstance(current_source_index, int) or current_source_index < 0:
                raise ManifestError(f"blocks[{index}].sourceIndex 必须是非负整数。")
            if current_source_index <= previous_source_image_index:
                raise ManifestError("文章图片及原图链接必须按 sourceIndex 严格递增排列。")
            previous_source_image_index = current_source_index
    if image_mode == "comic":
        generated_count = sum(block.get("type") == "image" for block in blocks)
        generated_failure_count = sum(block.get("type") == "generatedImageLink" for block in blocks)
        if generated_count + generated_failure_count != comic_count:
            raise ManifestError(
                f"comic 图片结果数量为 {generated_count + generated_failure_count}，但 comicCount 为 {comic_count}。"
            )
    return image_paths, validate_source_archive(data, manifest_path)


def unique_package_dir(result_dir: Path, title: str, now: datetime) -> Path:
    prefix = f"{now:%m%d}-{sanitize_filename(title)}"
    package_dir = result_dir / prefix
    if not package_dir.exists():
        return package_dir
    timed_prefix = f"{prefix}-{now:%H%M%S}"
    package_dir = result_dir / timed_prefix
    counter = 2
    while package_dir.exists():
        package_dir = result_dir / f"{timed_prefix}-{counter}"
        counter += 1
    return package_dir


def split_punctuation_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer: list[str] = []
    cleaned = text.strip()
    for index, char in enumerate(cleaned):
        if char in "\r\n":
            fragment = "".join(buffer).strip()
            if fragment:
                lines.append(fragment)
            buffer.clear()
            continue
        buffer.append(char)
        if char not in LINE_ENDINGS:
            continue
        previous = cleaned[index - 1] if index > 0 else ""
        following = cleaned[index + 1] if index + 1 < len(cleaned) else ""
        if char in ".," and previous.isdigit() and following.isdigit():
            continue
        if following in LINE_ENDINGS:
            continue
        fragment = "".join(buffer).strip()
        if fragment:
            lines.append(fragment)
        buffer.clear()
    fragment = "".join(buffer).strip()
    if fragment:
        lines.append(fragment)
    return lines


def sentence_line_markup(text: str) -> str:
    return "".join(f'<span class="sentence-line">{html.escape(line)}</span>' for line in split_punctuation_lines(text))


def paragraph_markup(text: str, preserve_line_breaks: bool = False) -> str:
    if preserve_line_breaks:
        return f"<p>{sentence_line_markup(text)}</p>"
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    return "\n".join(f"<p>{sentence_line_markup(part)}</p>" for part in paragraphs)


def build_html(
    data: dict,
    asset_dir_name: str,
    generated_at: str,
    source_archive_name: str | None = None,
) -> tuple[str, int, int]:
    fragments: list[str] = []
    image_number = 0
    failed_image_number = 0
    summary = str(data.get("summary") or "").strip()
    if summary:
        fragments.append(f'<p class="lead">{sentence_line_markup(summary)}</p>')
    preserve_ori_line_breaks = data.get("articleMode", "trans") == "ori"
    for block in data["blocks"]:
        block_type = block["type"]
        if block_type == "text":
            fragments.append(paragraph_markup(block["text"], preserve_ori_line_breaks))
        elif block_type == "heading":
            level = int(block.get("level", 2))
            fragments.append(f"<h{level}>{html.escape(block['text'].strip())}</h{level}>")
        elif block_type == "quote":
            fragments.append(f"<blockquote>{sentence_line_markup(block['text'].strip())}</blockquote>")
        elif block_type == "image":
            image_number += 1
            alt = html.escape(str(block.get("alt") or f"文章配图 {image_number}"), quote=True)
            src = f"{asset_dir_name}/image-{image_number:03d}.png"
            fragments.append(f'<figure><img src="{src}" alt="{alt}" loading="lazy"></figure>')
        elif block_type == "sourceImageLink":
            failed_image_number += 1
            raw_url = block.get("url")
            if isinstance(raw_url, str):
                escaped_url = html.escape(raw_url, quote=True)
                content = (
                    '原始图片链接：'
                    f'<a class="source-image-url" href="{escaped_url}" target="_blank" rel="noreferrer">'
                    f'{html.escape(raw_url)}</a>'
                )
            else:
                content = '原始图片链接：<span class="source-image-unavailable">不可用</span>'
            source_index = html.escape(str(block.get("sourceIndex")), quote=True)
            fragments.append(
                f'<figure class="source-image-link" data-source-index="{source_index}"><p>{content}</p></figure>'
            )
        elif block_type == "generatedImageLink":
            failed_image_number += 1
            error = html.escape(str(block.get("error") or "生成失败"))
            fragments.append(
                f'<figure class="source-image-link generated-image-link"><p>漫画图片{failed_image_number}：{error}</p></figure>'
            )

    title = html.escape(data["title"].strip())
    source_url = html.escape(data["sourceUrl"], quote=True)
    body = "\n".join(fragments)
    source_archive_meta = (
        f'  <meta name="source-archive" content="{html.escape(source_archive_name, quote=True)}">\n'
        if source_archive_name else ""
    )
    mode_meta = (
        f'  <meta name="article-mode" content="{html.escape(str(data.get("articleMode", "trans")), quote=True)}">\n'
        f'  <meta name="image-mode" content="{html.escape(str(data.get("imageMode", "copy")), quote=True)}">\n'
    )
    comic_meta = (
        f'  <meta name="comic-count" content="{int(data["comicCount"])}">\n'
        if data.get("imageMode") == "comic" else ""
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="source-url" content="{source_url}">
  <meta name="generated-at" content="{html.escape(generated_at, quote=True)}">
{mode_meta}{comic_meta}{source_archive_meta}  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; --ink:#26231f; --muted:#746f68; --paper:#fff; --accent:#d87754; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif; line-height:1.92; }}
    main {{ width:min(100%,760px); min-height:100vh; margin:0 auto; padding:clamp(34px,7vw,72px) clamp(22px,6vw,72px) 90px; background:var(--paper); box-shadow:0 12px 60px rgba(58,48,36,.10); }}
    header {{ margin-bottom:42px; padding-bottom:30px; border-bottom:1px solid #e9e1d6; }}
    h1 {{ margin:0; font-size:clamp(30px,6vw,46px); line-height:1.3; letter-spacing:.02em; text-align:center; }}
    .lead {{ margin:24px 0 0; color:var(--muted); font-size:18px; text-align:center; }}
    h2 {{ margin:56px 0 20px; font-size:27px; line-height:1.45; text-align:center; }}
    h3 {{ margin:40px 0 16px; font-size:22px; line-height:1.5; text-align:center; }}
    p {{ margin:0 0 22px; font-size:18px; text-align:center; overflow-wrap:anywhere; }}
    .sentence-line {{ display:block; text-align:center; }}
    blockquote {{ margin:34px 0; padding:20px 24px; border-left:5px solid var(--accent); background:#f8f1e8; border-radius:0 14px 14px 0; font-size:19px; font-weight:600; text-align:center; }}
    figure {{ margin:38px auto 42px; }}
    img {{ display:block; width:100%; height:auto; border-radius:18px; box-shadow:0 14px 36px rgba(55,42,28,.14); }}
    .source-image-link {{ padding:22px; border:1px dashed #d8cbbb; border-radius:14px; background:#faf7f1; overflow-wrap:anywhere; }}
    .source-image-link p {{ margin:0; color:var(--muted); font-size:15px; }}
    .source-image-link a {{ color:#9a553d; }}
    @media (max-width:600px) {{ main {{ padding-left:20px; padding-right:20px; box-shadow:none; }} p {{ font-size:17px; text-align:center; }} figure {{ margin-left:-4px; margin-right:-4px; }} }}
    @media print {{ main {{ width:100%; box-shadow:none; }} }}
  </style>
</head>
<body>
  <main>
    <article>
      <header><h1>{title}</h1></header>
      {body}
    </article>
  </main>
</body>
</html>
"""
    return document, image_number, failed_image_number


def source_image_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".jpeg":
        return ".jpg"
    return suffix or ".img"


def build_source_markdown(source_url: str, archive: dict, generated_at: str) -> tuple[str, list[str]]:
    lines = [
        f"# {archive['title']}",
        "",
        f"- 原文链接：<{source_url}>",
        f"- 归档时间：`{generated_at}`",
        "- 归档范围：已清理署名、二维码、广告、推广和无关装饰后的原文正文。",
        "",
        "## 原文正文",
        "",
    ]
    warnings: list[str] = []
    image_number = 0
    for block in archive["blocks"]:
        if block["type"] == "text":
            lines.extend([block["text"].strip(), ""])
            continue
        image_number += 1
        local_name = block.get("_localName")
        alt = str(block.get("alt") or f"原图 {image_number}").replace("[", "［").replace("]", "］")
        if local_name:
            lines.extend([f"![{alt}](source-images/{local_name})", ""])
        else:
            message = f"原图 {image_number} 本地保存失败"
            lines.extend([f"> {message}。", ""])
            warnings.append(block.get("_localWarning") or message)
        lines.extend([f"原始图片链接：<{block['url']}>", ""])
    return "\n".join(lines).rstrip() + "\n", warnings


def render(manifest_path: Path, result_dir: Path, now: datetime | None = None) -> dict:
    manifest_path = manifest_path.resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"无法读取 manifest：{exc}") from exc
    image_paths, source_archive = validate_manifest(data, manifest_path)
    rewritten_count = rewritten_character_count(data)
    source_count = data.get("sourceCharacterCount")
    rewrite_limit = (
        rewrite_character_limit(source_count)
        if data.get("articleMode", "trans") == "trans"
        and isinstance(source_count, int) and not isinstance(source_count, bool)
        else None
    )
    now = now or datetime.now(SHANGHAI_TZ)
    result_dir = result_dir.resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    package_dir = unique_package_dir(result_dir, data["title"], now)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{package_dir.name}-", dir=result_dir))
    html_name = f"{package_dir.name}.html"
    html_path = staging_dir / html_name
    assets_dir = staging_dir / "images"
    source_images_dir = staging_dir / "source-images"
    markdown_path = staging_dir / "source.md"
    archive_warnings: list[str] = []
    source_image_count = 0
    source_image_number = 0
    source_images_created = False
    try:
        if image_paths:
            assets_dir.mkdir()
            for index, source in enumerate(image_paths, start=1):
                shutil.copyfile(source, assets_dir / f"image-{index:03d}.png")

        if source_archive:
            for block in source_archive["blocks"]:
                if block["type"] != "image":
                    continue
                source_image_number += 1
                if not block.get("_resolvedPath"):
                    continue
                source_image_count += 1
                if not source_images_dir.exists():
                    source_images_dir.mkdir()
                local_name = f"image-{source_image_number:03d}{source_image_suffix(block['_resolvedPath'])}"
                shutil.copyfile(block["_resolvedPath"], source_images_dir / local_name)
                block["_localName"] = local_name
            markdown, archive_warnings = build_source_markdown(data["sourceUrl"], source_archive, now.isoformat())
            markdown_path.write_text(markdown, encoding="utf-8", newline="\n")

        document, image_count, failed_image_count = build_html(
            data,
            "images",
            now.isoformat(),
            "source.md" if source_archive else None,
        )
        html_path.write_text(document, encoding="utf-8", newline="\n")
        source_images_created = source_images_dir.exists()
        staging_dir.rename(package_dir)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    final_html = package_dir / html_name
    final_assets = package_dir / "images"
    final_markdown = package_dir / "source.md" if source_archive else None
    final_source_images = package_dir / "source-images" if source_images_created else None
    return {
        "packageDir": str(package_dir),
        "html": str(final_html),
        "markdown": str(final_markdown) if final_markdown else None,
        "images": str(final_assets),
        "assets": str(final_assets),
        "sourceImages": str(final_source_images) if final_source_images else None,
        "imageCount": image_count,
        "failedImageCount": failed_image_count,
        "sourceImageCount": source_image_count,
        "archiveWarnings": archive_warnings,
        "sourceCharacterCount": source_count,
        "rewrittenCharacterCount": rewritten_count,
        "rewriteCharacterLimit": rewrite_limit,
        "generatedAt": now.isoformat(),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path, default=Path("result"))
    args = parser.parse_args()
    try:
        print(json.dumps(render(args.manifest, args.result_dir), ensure_ascii=False, indent=2))
        return 0
    except (ManifestError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
