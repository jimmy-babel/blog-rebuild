#!/usr/bin/env python3
"""Compose a vertical PNG card from a new illustration and exact Chinese caption."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


WIDTH = 1080
HEIGHT = 1440
SCENE_HEIGHT = 900
DIVIDER_HEIGHT = 10
MAX_CAPTION_CHARACTERS = 20
ART_FONT_SCALE = 1.5
ART_FONT_SIZE_MAX = round(92 * ART_FONT_SCALE)
ART_FONT_SIZE_MIN = round(52 * ART_FONT_SCALE)
ART_FONT_SIZE_STEP = round(4 * ART_FONT_SCALE)
ART_FONT_NAMES = ("FZSTK.TTF", "STXINGKA.TTF", "STXINWEI.TTF")
PUNCTUATION = set("，。！？；,.!;?、：:‘’“”（）()【】[]《》")
PALETTES = {
    "喜": ("#FFF3C6", "#FFD36B", "#9B5A13"),
    "乐": ("#DDF7FF", "#68D6F4", "#135E75"),
    "怒": ("#FFE3DE", "#FF897A", "#832D26"),
    "哀": ("#E6EAF7", "#9BA9D5", "#3E4D7B"),
    "通用": ("#E8F5EE", "#8FD4AE", "#285D42"),
}


def load_font(
    size: int,
    bold: bool = False,
    artistic: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windows = Path("C:/Windows/Fonts")
    if artistic:
        candidates = [*(windows / name for name in ART_FONT_NAMES), windows / "msyh.ttc", windows / "simhei.ttf"]
    elif bold:
        candidates = [windows / "msyhbd.ttc", windows / "simhei.ttf", windows / "simsun.ttc"]
    else:
        candidates = [windows / "msyh.ttc", windows / "simhei.ttf", windows / "simsun.ttc"]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    scale = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize((math.ceil(image.width * scale), math.ceil(image.height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))


def gradient_background(top_color: str, accent: str) -> Image.Image:
    top = Image.new("RGB", (WIDTH, SCENE_HEIGHT), top_color)
    overlay = Image.new("RGB", (WIDTH, SCENE_HEIGHT), accent)
    mask = Image.new("L", (1, SCENE_HEIGHT))
    pixels = mask.load()
    for y in range(SCENE_HEIGHT):
        pixels[0, y] = int(28 + 72 * y / max(1, SCENE_HEIGHT - 1))
    top.paste(overlay, (0, 0), mask.resize((WIDTH, SCENE_HEIGHT)))
    return top


def fallback_scene(reference: Path, emotion: str) -> Image.Image:
    bg, accent, ink = PALETTES[emotion]
    scene = gradient_background(bg, accent)
    draw = ImageDraw.Draw(scene, "RGBA")
    for x, y, radius, alpha in ((120, 130, 90, 55), (930, 180, 125, 42), (160, 760, 150, 35), (910, 700, 80, 48)):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=accent + f"{alpha:02x}")
    draw.rounded_rectangle((80, 90, 1000, 820), radius=56, fill="#FFFFFFD9", outline=accent + "AA", width=5)

    with Image.open(reference) as source:
        portrait = cover(source, (690, 690))
    mask = Image.new("L", portrait.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, portrait.width, portrait.height), radius=86, fill=255)
    shadow = Image.new("RGBA", (portrait.width + 40, portrait.height + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((20, 20, portrait.width + 20, portrait.height + 20), radius=90, fill="#00000042")
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    scene.paste(shadow, (195, 105), shadow)
    scene.paste(portrait, (195, 105), mask)
    badge_font = load_font(32, bold=True)
    draw.rounded_rectangle((835, 720, 950, 785), radius=28, fill=ink + "E8")
    bbox = draw.textbbox((0, 0), emotion, font=badge_font)
    draw.text((892 - (bbox[2] - bbox[0]) / 2, 752 - (bbox[3] - bbox[1]) / 2 - bbox[1]), emotion, font=badge_font, fill="white")
    return scene


def normalized_caption(value: str) -> str:
    return re.sub(r"\s+", "", value)


def split_caption_lines(draw: ImageDraw.ImageDraw, caption: str, font: ImageFont.ImageFont) -> list[str]:
    if len(caption) <= 8 and draw.textlength(caption, font=font) <= 900:
        return [caption]
    candidates: list[tuple[float, list[str]]] = []
    for index in range(1, len(caption)):
        left, right = caption[:index], caption[index:]
        left_width = draw.textlength(left, font=font)
        right_width = draw.textlength(right, font=font)
        if max(left_width, right_width) > 900:
            continue
        penalty = abs(left_width - right_width)
        if right[0] in PUNCTUATION:
            penalty += 10000
        if left[-1] in "‘“（(【[《":
            penalty += 10000
        candidates.append((penalty, [left, right]))
    if not candidates:
        return [caption]
    return min(candidates, key=lambda item: item[0])[1]


def glyph_variation(caption: str, emotion: str, index: int, char: str) -> tuple[float, int]:
    digest = hashlib.sha256(f"{caption}|{emotion}|{index}|{char}".encode("utf-8")).digest()
    angle = -4.0 + digest[0] / 255 * 8.0
    if char in PUNCTUATION:
        angle *= 0.5
    y_offset = round(-7 + digest[1] / 255 * 14)
    return angle, y_offset


def render_glyph(char: str, font: ImageFont.ImageFont, color: str, angle: float) -> Image.Image:
    probe = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), char, font=font)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    margin = 24
    glyph = Image.new("RGBA", (width + margin * 2, height + margin * 2), (0, 0, 0, 0))
    ImageDraw.Draw(glyph).text((margin - bbox[0], margin - bbox[1]), char, font=font, fill=color)
    rotated = glyph.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    content_bbox = rotated.getbbox()
    return rotated.crop(content_bbox) if content_bbox else rotated


def render_art_line(text: str, font: ImageFont.ImageFont, color: str, caption: str, emotion: str) -> Image.Image:
    glyphs: list[tuple[Image.Image, int]] = []
    for index, char in enumerate(text):
        angle, y_offset = glyph_variation(caption, emotion, index, char)
        glyphs.append((render_glyph(char, font, color, angle), y_offset))
    tracking = 10
    width = sum(glyph.width for glyph, _ in glyphs) + tracking * max(0, len(glyphs) - 1)
    height = max(glyph.height + abs(offset) for glyph, offset in glyphs) + 16
    line = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    x = 0
    for glyph, offset in glyphs:
        y = (height - glyph.height) // 2 + offset
        line.alpha_composite(glyph, (x, y))
        x += glyph.width + tracking
    return line


def choose_art_layout(caption: str, emotion: str, color: str) -> list[Image.Image]:
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in range(ART_FONT_SIZE_MAX, ART_FONT_SIZE_MIN - 1, -ART_FONT_SIZE_STEP):
        font = load_font(size, artistic=True)
        line_texts = split_caption_lines(measure, caption, font)
        if len(line_texts) > 2:
            continue
        layers = [render_art_line(text, font, color, caption, emotion) for text in line_texts]
        if max(layer.width for layer in layers) <= 920 and sum(layer.height for layer in layers) + 34 * (len(layers) - 1) <= 380:
            return layers
    raise ValueError("卡片文案无法在两行内完整排版；请进一步精简。")


def compose(illustration: Path | None, reference: Path | None, caption: str, emotion: str, output: Path) -> bool:
    if emotion not in PALETTES:
        raise ValueError(f"未知情绪：{emotion}")
    if reference is None and (illustration is None or not illustration.is_file()):
        raise FileNotFoundError("没有品牌人物参考图，且缺少可用 AI 插画，无法生成卡片")
    if reference is not None and not reference.is_file():
        raise FileNotFoundError(f"角色参考图不存在：{reference}")
    caption = normalized_caption(caption)
    if not caption:
        raise ValueError("卡片文案不能为空。")
    if len(caption) > MAX_CAPTION_CHARACTERS:
        raise ValueError(f"卡片文案最多 {MAX_CAPTION_CHARACTERS} 个可见字符（包含标点）；请重新提炼。")

    used_fallback = True
    scene: Image.Image
    if illustration and illustration.is_file():
        try:
            with Image.open(illustration) as source:
                scene = cover(source, (WIDTH, SCENE_HEIGHT))
            used_fallback = False
        except OSError:
            if reference is None:
                raise OSError("AI 插画无法读取，且未提供品牌人物参考图")
            scene = fallback_scene(reference, emotion)
    else:
        if reference is None:
            raise FileNotFoundError("缺少 AI 插画，且未提供品牌人物参考图")
        scene = fallback_scene(reference, emotion)

    bg, accent, ink = PALETTES[emotion]
    card = Image.new("RGB", (WIDTH, HEIGHT), bg)
    card.paste(scene, (0, 0))
    draw = ImageDraw.Draw(card)
    draw.rectangle((0, SCENE_HEIGHT, WIDTH, HEIGHT), fill=bg)
    draw.rectangle((0, SCENE_HEIGHT, WIDTH, SCENE_HEIGHT + DIVIDER_HEIGHT - 1), fill=accent)

    layers = choose_art_layout(caption, emotion, ink)
    line_gap = 34
    total_height = sum(layer.height for layer in layers) + line_gap * (len(layers) - 1)
    usable_top = SCENE_HEIGHT + DIVIDER_HEIGHT
    y = usable_top + (HEIGHT - usable_top - total_height) // 2
    for layer in layers:
        x = (WIDTH - layer.width) // 2
        card.paste(layer, (x, y), layer)
        y += layer.height + line_gap

    output.parent.mkdir(parents=True, exist_ok=True)
    card.save(output, "PNG", optimize=True)
    return used_fallback


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--illustration", type=Path, help="AI 生成的无字场景图；缺失时使用模板回退")
    parser.add_argument("--reference", type=Path, help="对应情绪角色参考图（可选；无图时不得使用回退场景）")
    parser.add_argument("--caption", required=True, help="重新撰写的准确中文文案")
    parser.add_argument("--emotion", choices=tuple(PALETTES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        used_fallback = compose(args.illustration, args.reference, args.caption, args.emotion, args.output)
        print(json.dumps({"output": str(args.output.resolve()), "usedFallback": used_fallback}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
