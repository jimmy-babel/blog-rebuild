#!/usr/bin/env python3
"""Build the full renderer manifest from direct-fetch output and a compact article plan."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


class BuildError(ValueError):
    pass


def non_whitespace_count(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def load_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"无法读取 {label}：{exc}") from exc
    if not isinstance(data, dict):
        raise BuildError(f"{label} 顶层必须是对象。")
    return data


def is_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def source_index(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BuildError(f"{label} 必须是非负整数。")
    return value


def absolute_local_path(raw_path: str, base_dir: Path) -> str:
    path = Path(raw_path)
    return str((path if path.is_absolute() else base_dir / path).resolve())


def mode_value(article: dict, key: str, allowed: set[str], default: str) -> str:
    value = article.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        choices = "|".join(sorted(allowed))
        raise BuildError(f"article-plan.json.{key} 必须是 {choices}。")
    return value


def comic_count_value(article: dict, image_mode: str) -> int:
    value = article.get("comicCount", 5)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BuildError("article-plan.json.comicCount 必须是正整数。")
    if image_mode != "comic" and "comicCount" in article:
        raise BuildError("comicCount 仅可用于 imageMode=comic。")
    return value


def build(source_path: Path, article_path: Path) -> tuple[dict, list[str]]:
    source_path = source_path.resolve()
    article_path = article_path.resolve()
    source = load_json(source_path, "source.json")
    article = load_json(article_path, "article-plan.json")

    article_mode = mode_value(article, "articleMode", {"trans", "ori"}, "trans")
    image_mode = mode_value(article, "imageMode", {"copy", "rebuild", "comic"}, "copy")
    comic_count = comic_count_value(article, image_mode)
    if image_mode == "comic" and article_mode != "ori":
        raise BuildError("imageMode=comic 必须搭配 articleMode=ori。")

    if source.get("success") is not True:
        raise BuildError("source.json 不是成功的抓取结果。")
    source_url = source.get("sourceUrl")
    if not is_https_url(source_url) or urlparse(source_url).hostname != "mp.weixin.qq.com":
        raise BuildError("source.json.sourceUrl 必须是 HTTPS 微信文章链接。")
    source_title = source.get("title")
    if not isinstance(source_title, str) or not source_title.strip():
        raise BuildError("source.json.title 不能为空。")
    raw_source_blocks = source.get("blocks")
    if not isinstance(raw_source_blocks, list) or not raw_source_blocks:
        raise BuildError("source.json.blocks 必须是非空数组。")

    indexed_source: dict[int, dict] = {}
    ordered_source: list[tuple[int, dict]] = []
    for position, block in enumerate(raw_source_blocks):
        if not isinstance(block, dict):
            raise BuildError(f"source.json.blocks[{position}] 必须是对象。")
        index = source_index(block.get("sourceIndex"), f"source.json.blocks[{position}].sourceIndex")
        if index in indexed_source:
            raise BuildError(f"source.json 包含重复 sourceIndex：{index}")
        if block.get("type") not in {"text", "image"}:
            continue
        indexed_source[index] = block
        ordered_source.append((index, block))
    ordered_source.sort(key=lambda item: item[0])

    removed_raw = article.get("removedSourceIndexes", [])
    if not isinstance(removed_raw, list):
        raise BuildError("removedSourceIndexes 必须是数组。")
    removed: set[int] = set()
    for position, raw_index in enumerate(removed_raw):
        index = source_index(raw_index, f"removedSourceIndexes[{position}]")
        if index not in indexed_source:
            raise BuildError(f"removedSourceIndexes 包含不存在的 sourceIndex：{index}")
        removed.add(index)

    warnings: list[str] = []
    archive_blocks: list[dict] = []
    retained_image_indexes: list[int] = []
    for index, block in ordered_source:
        if index in removed:
            continue
        if block["type"] == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                archive_blocks.append({"type": "text", "sourceIndex": index, "text": text})
            continue

        retained_image_indexes.append(index)
        url = block.get("url")
        if not is_https_url(url):
            warnings.append(f"原图 sourceIndex={index} 缺少有效 HTTPS URL，未写入 source.md 归档。")
            continue
        archived = {
            "type": "image",
            "sourceIndex": index,
            "url": url,
            "alt": str(block.get("alt") or ""),
        }
        local_path = block.get("localPath") or block.get("path")
        if isinstance(local_path, str) and local_path.strip():
            archived["path"] = absolute_local_path(local_path, source_path.parent)
        archive_blocks.append(archived)

    if not archive_blocks:
        raise BuildError("二次清洗后没有可归档的正文块。")
    source_character_count = sum(
        non_whitespace_count(block["text"])
        for block in archive_blocks
        if block["type"] == "text"
    )
    # Image-only articles are valid: the model can inspect the archived images
    # directly when rewriting, so the textual source count is intentionally 0.

    title = article.get("title")
    if not isinstance(title, str) or not title.strip():
        raise BuildError("article-plan.json.title 不能为空。")
    summary = article.get("summary")
    if summary is not None and (not isinstance(summary, str) or not summary.strip()):
        raise BuildError("article-plan.json.summary 必须是非空字符串或省略。")
    raw_article_blocks = article.get("blocks")
    if not isinstance(raw_article_blocks, list) or not raw_article_blocks:
        raise BuildError("article-plan.json.blocks 必须是非空数组。")

    article_blocks: list[dict] = []
    handled_images: list[int] = []
    for position, block in enumerate(raw_article_blocks):
        if not isinstance(block, dict):
            raise BuildError(f"article-plan.json.blocks[{position}] 必须是对象。")
        block_type = block.get("type")
        if block_type in {"text", "heading", "quote"}:
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                raise BuildError(f"article-plan.json.blocks[{position}].text 不能为空。")
            rewritten = {"type": block_type, "text": text}
            if block_type == "heading":
                rewritten["level"] = block.get("level", 2)
            article_blocks.append(rewritten)
            continue
        if image_mode == "comic" and block_type in {"image", "generatedImageLink"}:
            rewritten = {
                "type": block_type,
                "alt": str(block.get("alt") or ""),
            }
            if block_type == "image":
                raw_path = block.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    raise BuildError(f"comic 图片 blocks[{position}] 缺少本地 path。")
                rewritten["path"] = absolute_local_path(raw_path, article_path.parent)
            else:
                error = block.get("error")
                if error is not None:
                    if not isinstance(error, str) or not error.strip():
                        raise BuildError(f"comic 失败图片 blocks[{position}].error 必须是非空字符串。")
                    rewritten["error"] = error.strip()
            article_blocks.append(rewritten)
            continue
        if image_mode == "comic" and block_type == "sourceImageLink":
            raise BuildError("imageMode=comic 的失败结果必须使用 generatedImageLink。")
        if block_type not in {"image", "sourceImageLink"}:
            raise BuildError(f"article-plan.json.blocks[{position}].type 不受支持：{block_type}")

        index = source_index(block.get("sourceIndex"), f"article-plan.json.blocks[{position}].sourceIndex")
        source_block = indexed_source.get(index)
        if not source_block or source_block.get("type") != "image" or index in removed:
            raise BuildError(f"图片结果引用了未保留的来源图片 sourceIndex={index}。")
        if index in handled_images:
            raise BuildError(f"来源图片 sourceIndex={index} 被重复处理。")
        handled_images.append(index)
        rewritten = {
            "type": block_type,
            "sourceIndex": index,
            "alt": str(block.get("alt") or source_block.get("alt") or ""),
        }
        if block_type == "image":
            raw_path = block.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise BuildError(f"成功图片 sourceIndex={index} 缺少本地 path。")
            rewritten["path"] = absolute_local_path(raw_path, article_path.parent)
        else:
            url = source_block.get("url")
            if is_https_url(url):
                rewritten["url"] = url
            else:
                warnings.append(f"失败图片 sourceIndex={index} 没有可用原始图片链接。")
        article_blocks.append(rewritten)

    if image_mode == "comic":
        generated_count = sum(block["type"] == "image" for block in article_blocks)
        generated_failure_count = sum(block["type"] == "generatedImageLink" for block in article_blocks)
        if generated_count + generated_failure_count != comic_count:
            raise BuildError(
                f"comic 结果数量为 {generated_count + generated_failure_count}，但 comicCount 为 {comic_count}。"
            )
    elif handled_images != sorted(handled_images):
        raise BuildError("文章中的图片结果必须按来源 sourceIndex 保持相对顺序。")
    if image_mode != "comic" and set(handled_images) != set(retained_image_indexes):
        missing = sorted(set(retained_image_indexes) - set(handled_images))
        extra = sorted(set(handled_images) - set(retained_image_indexes))
        raise BuildError(f"每张保留原图必须有且仅有一个结果；缺少={missing}，多余={extra}。")

    manifest = {
        "title": title.strip(),
        "sourceUrl": source_url,
        "articleMode": article_mode,
        "imageMode": image_mode,
        "sourceCharacterCount": source_character_count,
        "sourceArchive": {"title": source_title.strip(), "blocks": archive_blocks},
        "blocks": article_blocks,
    }
    if isinstance(summary, str):
        manifest["summary"] = summary.strip()
    if image_mode == "comic":
        manifest["comicCount"] = comic_count
    return manifest, warnings


def write_manifest(source_path: Path, article_path: Path, output_path: Path) -> dict:
    manifest, warnings = build(source_path, article_path)
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=output_path.parent,
            prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            json.dump(manifest, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        temporary_path.replace(output_path)
    except Exception:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
        raise
    return {
        "manifest": str(output_path),
        "sourceCharacterCount": manifest["sourceCharacterCount"],
        "imageCount": sum(block["type"] == "image" for block in manifest["blocks"]),
        "sourceImageLinkCount": sum(block["type"] == "sourceImageLink" for block in manifest["blocks"]),
        "generatedImageLinkCount": sum(block["type"] == "generatedImageLink" for block in manifest["blocks"]),
        "warnings": warnings,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--article", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(write_manifest(args.source, args.article, args.output), ensure_ascii=False, indent=2))
        return 0
    except (BuildError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
