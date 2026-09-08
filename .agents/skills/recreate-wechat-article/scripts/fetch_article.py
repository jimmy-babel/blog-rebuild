#!/usr/bin/env python3
"""Call the local n8n extractor and optionally cache source images for review."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
)


class FetchFailure(RuntimeError):
    pass


def validate_wechat_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com" or not parsed.path.startswith("/s/"):
        raise FetchFailure("仅支持 https://mp.weixin.qq.com/s/... 格式的文章链接。")
    return value.strip()


def load_config(script_dir: Path, override: Path | None) -> dict:
    config_path = override or script_dir.parent / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FetchFailure(f"无法读取 skill 配置：{config_path} ({exc})") from exc


def post_json(url: str, payload: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
            message = detail.get("message") or detail.get("error") or body
        except json.JSONDecodeError:
            message = body or str(exc)
        if exc.code in (422, 502):
            raise FetchFailure(
                f"n8n 返回 HTTP {exc.code}：{message}。微信页面可能触发反爬或正文不可解析；请提供导出的 HTML/PDF。"
            ) from exc
        raise FetchFailure(f"n8n 返回 HTTP {exc.code}：{message}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchFailure(
            "无法连接本机 n8n。请先启动 Docker/n8n，并确认 config.json 中的 Webhook 地址可访问。"
        ) from exc

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchFailure("n8n 返回了非 JSON 响应。") from exc
    if not result.get("success"):
        raise FetchFailure(str(result.get("message") or result.get("error") or "提取失败"))
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        raise FetchFailure("n8n 响应缺少 blocks。")
    if not any(isinstance(block, dict) and block.get("type") in {"text", "image"} for block in blocks):
        raise FetchFailure("n8n 响应没有可用文字或图片内容。")
    return result


def extension_for(content_type: str, url: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    explicit = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(normalized)
    if explicit:
        return explicit
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return mimetypes.guess_extension(normalized) or ".img"


def download_images(article: dict, work_dir: Path, timeout: int) -> list[str]:
    image_dir = work_dir / "source-images"
    image_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    image_number = 0
    for block in article["blocks"]:
        if block.get("type") != "image" or not block.get("url"):
            continue
        image_number += 1
        request = urllib.request.Request(
            block["url"],
            headers={"User-Agent": USER_AGENT, "Referer": article.get("sourceUrl", "")},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
            if not payload:
                raise OSError("empty response")
            target = image_dir / f"image-{image_number:03d}{extension_for(content_type, block['url'])}"
            target.write_bytes(payload)
            block["localPath"] = target.relative_to(work_dir).as_posix()
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            warnings.append(f"图片 {image_number} 下载失败：{exc}")
    return warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="微信公众号文章 URL")
    parser.add_argument("--work-dir", type=Path, required=True, help="唯一临时工作目录")
    parser.add_argument("--config", type=Path, help="覆盖默认 config.json")
    parser.add_argument("--download-images", action="store_true", help="下载来源图片供语义检查")
    parser.add_argument("--force", action="store_true", help="允许覆盖工作目录中的 source.json")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        source_url = validate_wechat_url(args.url)
        config = load_config(Path(__file__).resolve().parent, args.config)
        timeout = int(config.get("timeoutSeconds", 45))
        output = args.work_dir / "source.json"
        if output.exists() and not args.force:
            raise FetchFailure(f"拒绝覆盖已有临时结果：{output}；请换一个工作目录或使用 --force。")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        article = post_json(str(config["webhookUrl"]), {"url": source_url}, timeout)
        warnings = download_images(article, args.work_dir, timeout) if args.download_images else []
        article["downloadWarnings"] = warnings
        output.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "sourceJson": str(output.resolve()),
            "title": article.get("title", ""),
            "textBlocks": sum(block.get("type") == "text" for block in article["blocks"]),
            "imageBlocks": sum(block.get("type") == "image" for block in article["blocks"]),
            "removedBlocks": len(article.get("removedBlocks", [])),
            "downloadWarnings": warnings,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (FetchFailure, KeyError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
