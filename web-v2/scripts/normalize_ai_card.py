#!/usr/bin/env python3
"""Normalize an AI-rendered card to the project's exact size and pale background."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps


TARGET_SIZE = (1122, 1402)
TARGET_BACKGROUND = (247, 246, 246)
TARGET_BACKGROUND_HEX = "#F7F6F6"
BACKGROUND_THRESHOLD = 18


class NormalizeError(ValueError):
    pass


def _edge_seeds(width: int, height: int) -> tuple[tuple[int, int], ...]:
    return (
        (0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1),
        (width // 2, 0), (width // 2, height - 1), (0, height // 2), (width - 1, height // 2),
    )


def recolor_connected_background(image: Image.Image) -> Image.Image:
    recolored = image.convert("RGB")
    for seed in _edge_seeds(*recolored.size):
        ImageDraw.floodfill(recolored, seed, TARGET_BACKGROUND, thresh=BACKGROUND_THRESHOLD)
    return recolored


def _expand_interval(start: int, end: int, desired: int, limit: int) -> tuple[int, int]:
    current = end - start
    if desired <= current:
        return start, end
    missing = desired - current
    start -= missing // 2
    end += missing - missing // 2
    if start < 0:
        end = min(limit, end - start)
        start = 0
    if end > limit:
        start = max(0, start - (end - limit))
        end = limit
    return start, end


def compact_crop_box(image: Image.Image) -> tuple[int, int, int, int]:
    background = Image.new("RGB", image.size, TARGET_BACKGROUND)
    difference = ImageChops.difference(image, background).convert("L")
    content_mask = difference.point(lambda value: 255 if value > BACKGROUND_THRESHOLD else 0)
    bbox = content_mask.getbbox()
    if bbox is None:
        return 0, 0, image.width, image.height

    left, top, right, bottom = bbox
    margin_x = max(12, round(image.width * 0.025))
    margin_y = max(12, round(image.height * 0.020))
    left = max(0, left - margin_x)
    top = max(0, top - margin_y)
    right = min(image.width, right + margin_x)
    bottom = min(image.height, bottom + margin_y)

    target_ratio = TARGET_SIZE[0] / TARGET_SIZE[1]
    crop_width = right - left
    crop_height = bottom - top
    if crop_width / crop_height < target_ratio:
        desired_width = math.ceil(crop_height * target_ratio)
        if desired_width <= image.width:
            left, right = _expand_interval(left, right, desired_width, image.width)
    else:
        desired_height = math.ceil(crop_width / target_ratio)
        if desired_height <= image.height:
            top, bottom = _expand_interval(top, bottom, desired_height, image.height)
    return left, top, right, bottom


def normalize(input_path: Path, output_path: Path) -> dict:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file():
        raise NormalizeError(f"AI 图片不存在：{input_path}")
    try:
        with Image.open(input_path) as opened:
            source_format = opened.format
            source_size = opened.size
            if opened.width <= 0 or opened.height <= 0:
                raise NormalizeError("AI 图片尺寸为空。")
            recolored = recolor_connected_background(opened)
    except OSError as exc:
        raise NormalizeError(f"AI 图片无法读取：{exc}") from exc

    crop_box = compact_crop_box(recolored)
    cropped = recolored.crop(crop_box)
    target_ratio = TARGET_SIZE[0] / TARGET_SIZE[1]
    crop_ratio = cropped.width / cropped.height
    if abs(crop_ratio - target_ratio) <= 0.01:
        final = cropped.resize(TARGET_SIZE, Image.Resampling.LANCZOS)
    else:
        contained = ImageOps.contain(cropped, TARGET_SIZE, Image.Resampling.LANCZOS)
        final = Image.new("RGB", TARGET_SIZE, TARGET_BACKGROUND)
        final.paste(contained, ((TARGET_SIZE[0] - contained.width) // 2, (TARGET_SIZE[1] - contained.height) // 2))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        final.save(temporary_path, format="PNG", optimize=True)
        temporary_path.replace(output_path)
    except Exception:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
        raise

    return {
        "input": str(input_path),
        "output": str(output_path),
        "inputSize": list(source_size),
        "outputSize": list(TARGET_SIZE),
        "background": TARGET_BACKGROUND_HEX,
        "cropBox": list(crop_box),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(normalize(args.input, args.output), ensure_ascii=False, indent=2))
        return 0
    except (NormalizeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
