#!/usr/bin/env python3
"""Validate an offline article package, rewritten images, and source archive."""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[str] = []
        self.source_image_links: list[str | None] = []
        self.generated_image_links: int = 0
        self.media_sequence: list[str] = []
        self.source_url = ""
        self.source_archive = ""
        self.article_mode = "trans"
        self.image_mode = "copy"
        self.comic_count: int | None = None
        self.title_depth = 0
        self.title_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "img" and values.get("src"):
            self.images.append(values["src"] or "")
            self.media_sequence.append("image")
        classes = set((values.get("class") or "").split())
        if tag == "a" and "source-image-url" in classes:
            self.source_image_links.append(values.get("href"))
            self.media_sequence.append("sourceImageLink")
        if tag == "span" and "source-image-unavailable" in classes:
            self.source_image_links.append(None)
            self.media_sequence.append("sourceImageLink")
        if tag == "figure" and "generated-image-link" in classes:
            self.generated_image_links += 1
            self.media_sequence.append("generatedImageLink")
        if tag == "meta" and values.get("name") == "source-url":
            self.source_url = values.get("content") or ""
        if tag == "meta" and values.get("name") == "source-archive":
            self.source_archive = values.get("content") or ""
        if tag == "meta" and values.get("name") == "article-mode":
            self.article_mode = values.get("content") or ""
        if tag == "meta" and values.get("name") == "image-mode":
            self.image_mode = values.get("content") or ""
        if tag == "meta" and values.get("name") == "comic-count":
            raw_count = values.get("content") or ""
            try:
                self.comic_count = int(raw_count)
            except ValueError:
                self.comic_count = None
        if tag == "title":
            self.title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.title_depth:
            self.title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_text.append(data)


def parse_expected_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)[xX×](\d+)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("尺寸必须使用 WIDTHxHEIGHT，例如 1122x1402。")
    width, height = (int(part) for part in match.groups())
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("图片尺寸必须为正整数。")
    return width, height


def validate(html_path: Path, expected_image_size: tuple[int, int] | None = None) -> dict:
    html_path = html_path.resolve()
    errors: list[str] = []
    try:
        text = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"valid": False, "html": str(html_path), "errors": [str(exc)]}

    parser = ArticleParser()
    parser.feed(text)
    title = "".join(parser.title_text).strip()
    if not title:
        errors.append("HTML 缺少非空 title。")
    if html_path.parent.name != html_path.stem:
        errors.append("HTML 文件名必须与文章目录同名。")
    parsed_source = urlparse(parser.source_url)
    if parsed_source.scheme != "https" or parsed_source.hostname != "mp.weixin.qq.com":
        errors.append("HTML 缺少有效的微信 source-url 元数据。")

    if parser.article_mode not in {"trans", "ori"}:
        errors.append("HTML 缺少有效的 article-mode 元数据。")
    if parser.image_mode not in {"copy", "rebuild", "comic"}:
        errors.append("HTML 缺少有效的 image-mode 元数据。")
    if parser.image_mode == "comic":
        if parser.article_mode != "ori":
            errors.append("comic 必须使用 ori article-mode。")
        if parser.comic_count is None or parser.comic_count <= 0:
            errors.append("comic 缺少有效的 comic-count 元数据。")
        elif len(parser.images) + parser.generated_image_links != parser.comic_count:
            actual = len(parser.images) + parser.generated_image_links
            errors.append(f"comic 图片数量为 {actual}，期望 {parser.comic_count}")

    checked: list[dict] = []
    for src in parser.images:
        parsed = urlparse(src)
        if parsed.scheme or parsed.netloc or src.startswith(("/", "\\")):
            errors.append(f"图片不是相对本地路径：{src}")
            continue
        if not src.startswith("images/"):
            errors.append(f"成品图片必须位于 images/：{src}")
        target = (html_path.parent / Path(src)).resolve()
        try:
            target.relative_to(html_path.parent)
        except ValueError:
            errors.append(f"图片路径越界：{src}")
            continue
        if not target.is_file():
            errors.append(f"图片断链：{src}")
            continue
        try:
            with Image.open(target) as image:
                width, height = image.size
                image_format = image.format
            if image_format != "PNG":
                errors.append(f"图片不是 PNG：{src}")
            if height <= width:
                errors.append(f"图片不是纵向布局：{src} ({width}x{height})")
            if expected_image_size and (width, height) != expected_image_size:
                errors.append(
                    f"图片尺寸不符合当前文案模式：{src} ({width}x{height})，"
                    f"期望 {expected_image_size[0]}x{expected_image_size[1]}"
                )
            checked.append({"src": src, "width": width, "height": height, "format": image_format})
        except OSError as exc:
            errors.append(f"图片无法读取：{src} ({exc})")

    checked_source_links: list[dict] = []
    for url in parser.source_image_links:
        if url is None:
            checked_source_links.append({"url": None, "available": False})
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append(f"正文原始图片链接不是 HTTPS：{url}")
        checked_source_links.append({"url": url, "available": True})

    assets_dir = html_path.parent / "images"
    if parser.images and not assets_dir.is_dir():
        errors.append("缺少 images 目录。")
    if assets_dir.is_dir():
        actual_assets = sorted(path.name for path in assets_dir.iterdir() if path.is_file())
        expected_assets = [f"image-{index:03d}.png" for index in range(1, len(parser.images) + 1)]
        if actual_assets != expected_assets:
            errors.append(f"images 文件不匹配：实际 {actual_assets}，期望 {expected_assets}")

    markdown_info: dict | None = None
    if parser.source_archive:
        if parser.source_archive != "source.md":
            errors.append("source-archive 元数据必须指向当前目录的 source.md。")
        markdown_path = (html_path.parent / "source.md").resolve()
        if not markdown_path.is_file():
            errors.append("缺少 source.md。")
        else:
            try:
                markdown = markdown_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"source.md 无法读取：{exc}")
                markdown = ""
            if f"<{parser.source_url}>" not in markdown:
                errors.append("source.md 缺少原文链接。")
            local_refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)
            original_urls = re.findall(r"^原始图片链接：<([^>]+)>$", markdown, flags=re.MULTILINE)
            failed_count = len(re.findall(r"^> 原图 \d+ 本地保存失败。$", markdown, flags=re.MULTILINE))
            if len(original_urls) != len(local_refs) + failed_count:
                errors.append("source.md 的原图记录、本地图片和失败标记数量不一致。")
            ordered_urls = re.findall(
                r"^(?:!\[[^\]]*\]\([^)]+\)|> 原图 \d+ 本地保存失败。)\n\n原始图片链接：<([^>]+)>$",
                markdown,
                flags=re.MULTILINE,
            )
            if ordered_urls != original_urls:
                errors.append("source.md 必须在每张本地原图或失败标记后紧接对应原始 URL。")
            for url in original_urls:
                parsed_url = urlparse(url)
                if parsed_url.scheme != "https" or not parsed_url.netloc:
                    errors.append(f"原始图片链接不是 HTTPS：{url}")

            source_images: list[dict] = []
            expected_source_files: list[str] = []
            for ref in local_refs:
                parsed_ref = urlparse(ref)
                if parsed_ref.scheme or parsed_ref.netloc or not ref.startswith("source-images/"):
                    errors.append(f"原图不是 source-images/ 下的本地相对路径：{ref}")
                    continue
                target = (html_path.parent / Path(ref)).resolve()
                try:
                    target.relative_to(html_path.parent)
                except ValueError:
                    errors.append(f"原图路径越界：{ref}")
                    continue
                if not target.is_file():
                    errors.append(f"原图断链：{ref}")
                    continue
                try:
                    with Image.open(target) as image:
                        source_images.append({
                            "src": ref,
                            "width": image.width,
                            "height": image.height,
                            "format": image.format,
                        })
                    expected_source_files.append(Path(ref).name)
                except OSError as exc:
                    errors.append(f"原图无法读取：{ref} ({exc})")

            source_images_dir = html_path.parent / "source-images"
            if expected_source_files:
                if not source_images_dir.is_dir():
                    errors.append("缺少 source-images 目录。")
                else:
                    actual_source_files = sorted(path.name for path in source_images_dir.iterdir() if path.is_file())
                    if actual_source_files != sorted(expected_source_files):
                        errors.append(
                            f"source-images 文件不匹配：实际 {actual_source_files}，期望 {sorted(expected_source_files)}"
                        )
            elif source_images_dir.exists():
                errors.append("没有本地原图时不应创建 source-images 目录。")
            markdown_info = {
                "path": str(markdown_path),
                "originalImageUrls": original_urls,
                "sourceImages": source_images,
                "failedImageCount": failed_count,
            }

    return {
        "valid": not errors,
        "html": str(html_path),
        "title": title,
        "sourceUrl": parser.source_url,
        "articleMode": parser.article_mode,
        "imageMode": parser.image_mode,
        "comicCount": parser.comic_count,
        "generatedImageLinkCount": parser.generated_image_links,
        "images": checked,
        "sourceImageLinks": checked_source_links,
        "mediaSequence": parser.media_sequence,
        "expectedImageSize": list(expected_image_size) if expected_image_size else None,
        "sourceArchive": markdown_info,
        "errors": errors,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    parser.add_argument("--expected-image-size", type=parse_expected_size)
    args = parser.parse_args()
    result = validate(args.html, args.expected_image_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
