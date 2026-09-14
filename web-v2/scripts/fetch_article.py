#!/usr/bin/env python3
"""Fetch a WeChat article directly and turn its rendered body into source blocks."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
)
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class FetchFailure(RuntimeError):
    pass


def validate_wechat_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com" or not parsed.path.startswith("/s/"):
        raise FetchFailure("仅支持 https://mp.weixin.qq.com/s/... 格式的文章链接。")
    return value.strip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


class ArticleParser(HTMLParser):
    """Small dependency-free parser for the stable WeChat article DOM."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.blocks: list[dict] = []
        self._title_depth = 0
        self._body_depth = 0
        self._skip_depth = 0
        self._text_parts: list[str] = []

    @property
    def in_body(self) -> bool:
        return self._body_depth > 0 and self._skip_depth == 0

    def _flush_text(self) -> None:
        text = _clean_text("".join(self._text_parts))
        self._text_parts.clear()
        if text:
            self.blocks.append({"type": "text", "text": text})

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._title_depth += 1
        is_body_root = attributes.get("id") == "js_content" or "rich_media_content" in (attributes.get("class") or "")
        if is_body_root and self._body_depth == 0:
            self._body_depth += 1
            return
        if self._body_depth and tag not in VOID_TAGS:
            self._body_depth += 1
        if self.in_body and tag == "img":
            self._flush_text()
            image_url = attributes.get("data-src") or attributes.get("src")
            if image_url:
                self.blocks.append({"type": "image", "url": image_url, "alt": _clean_text(attributes.get("alt") or "")})
        elif self.in_body and tag in {"p", "section", "div", "h1", "h2", "h3", "li", "blockquote", "br"}:
            self._flush_text()

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if self.in_body and tag in {"p", "section", "div", "h1", "h2", "h3", "li", "blockquote", "br"}:
            self._flush_text()
        if self._body_depth and tag not in VOID_TAGS:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title_parts.append(data)
        if self.in_body:
            self._text_parts.append(data)


def fetch_html(url: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise FetchFailure(f"微信页面返回 HTTP {exc.code}，可能触发反爬或链接失效；请提供导出的 HTML/PDF。") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchFailure(f"无法直接获取微信文章页面：{exc}") from exc
    if not raw:
        raise FetchFailure("微信页面为空。")
    return raw.decode(charset, errors="replace")


def _meta_title(source: str) -> str:
    match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', source, re.I)
    return _clean_text(match.group(1)) if match else ""


def parse_article(source_url: str, source: str) -> dict:
    parser = ArticleParser()
    parser.feed(source)
    parser._flush_text()
    blocks: list[dict] = []
    for block in parser.blocks:
        if block["type"] == "image":
            block["url"] = urllib.parse.urljoin(source_url, block["url"])
        if not blocks or block["type"] != blocks[-1]["type"] or block["type"] == "image":
            blocks.append(block)
        elif block["type"] == "text":
            # Keep the line boundary created by <br> or a block-level element.
            # Whitespace inside each individual text node is still normalized by
            # _clean_text, so HTML indentation does not create empty lines.
            blocks[-1]["text"] += "\n" + block["text"]
    title = _meta_title(source) or _clean_text("".join(parser.title_parts))
    if not blocks:
        raise FetchFailure("页面中没有找到可用正文或图片；请提供导出的 HTML/PDF。")
    for source_index, block in enumerate(blocks):
        block["sourceIndex"] = source_index
    return {"success": True, "sourceUrl": source_url, "title": title or "未命名文章", "blocks": blocks}


def extension_for(content_type: str, url: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    explicit = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    if normalized in explicit:
        return explicit[normalized]
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return (".jpg" if suffix == ".jpeg" else suffix) if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else mimetypes.guess_extension(normalized) or ".img"


def download_images(article: dict, work_dir: Path, timeout: int) -> list[str]:
    image_dir = work_dir / "source-images"
    image_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    number = 0
    for block in article["blocks"]:
        if block.get("type") != "image" or not block.get("url"):
            continue
        number += 1
        request = urllib.request.Request(block["url"], headers={"User-Agent": USER_AGENT, "Referer": article["sourceUrl"]})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
            if not payload:
                raise OSError("empty response")
            target = image_dir / f"image-{number:03d}{extension_for(content_type, block['url'])}"
            target.write_bytes(payload)
            block["localPath"] = target.relative_to(work_dir).as_posix()
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            warnings.append(f"图片 {number} 下载失败：{exc}")
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="兼容旧调用；新流程只读取 timeoutSeconds")
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        source_url = validate_wechat_url(args.url)
        timeout = 45
        if args.config and args.config.exists():
            timeout = int(json.loads(args.config.read_text(encoding="utf-8")).get("timeoutSeconds", timeout))
        output = args.work_dir / "source.json"
        if output.exists() and not args.force:
            raise FetchFailure(f"拒绝覆盖已有临时结果：{output}；请换一个工作目录或使用 --force。")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        article = parse_article(source_url, fetch_html(source_url, timeout))
        warnings = download_images(article, args.work_dir, timeout) if args.download_images else []
        article["downloadWarnings"] = warnings
        output.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"sourceJson": str(output.resolve()), "title": article["title"], "textBlocks": sum(b["type"] == "text" for b in article["blocks"]), "imageBlocks": sum(b["type"] == "image" for b in article["blocks"]), "downloadWarnings": warnings}, ensure_ascii=False, indent=2))
        return 0
    except (FetchFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
