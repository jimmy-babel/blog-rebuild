from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "recreate-wechat-article" / "scripts"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


choose_emotion = load_module("choose_emotion")
compose_card = load_module("compose_card")
fetch_article = load_module("fetch_article")
build_manifest = load_module("build_manifest")
normalize_ai_card = load_module("normalize_ai_card")
render_article = load_module("render_article")
validate_output = load_module("validate_output")


class FetchTests(unittest.TestCase):
    def test_invalid_wechat_url(self):
        with self.assertRaises(fetch_article.FetchFailure):
            fetch_article.validate_wechat_url("https://example.com/article")

    def test_n8n_offline_message(self):
        with self.assertRaisesRegex(fetch_article.FetchFailure, "启动 Docker/n8n"):
            fetch_article.post_json("http://127.0.0.1:9/unreachable", {"url": "x"}, 1)

    def test_image_only_n8n_response_is_accepted(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "success": True,
            "contentMode": "image_only",
            "blocks": [{"type": "image", "sourceIndex": 0, "url": "https://mmbiz.qpic.cn/a.jpg"}],
        }).encode("utf-8")
        response.__enter__.return_value = response
        with mock.patch.object(fetch_article.urllib.request, "urlopen", return_value=response):
            result = fetch_article.post_json("https://example.test/webhook", {"url": "x"}, 1)
        self.assertEqual(result["contentMode"], "image_only")
        self.assertEqual(result["blocks"][0]["type"], "image")

    def test_response_without_text_or_images_is_rejected(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"success": True, "blocks": []}).encode("utf-8")
        response.__enter__.return_value = response
        with mock.patch.object(fetch_article.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(fetch_article.FetchFailure, "没有可用文字或图片"):
                fetch_article.post_json("https://example.test/webhook", {"url": "x"}, 1)


class EmotionTests(unittest.TestCase):
    def test_ambiguous_choice_is_stable_and_limited(self):
        url = "https://mp.weixin.qq.com/s/example"
        first = choose_emotion.choose_emotion(url, 3)
        self.assertEqual(first, choose_emotion.choose_emotion(url, 3))
        self.assertIn(first, {"喜", "乐", "通用"})


class SkillContractTests(unittest.TestCase):
    def test_image_modes_are_rebuild_and_copy_with_copy_as_default(self):
        files = [
            ROOT / "AGENTS.md",
            ROOT / ".agents" / "skills" / "recreate-wechat-article" / "SKILL.md",
            ROOT / ".agents" / "skills" / "recreate-wechat-article" / "references" / "image-generation-modes.md",
            ROOT / ".agents" / "skills" / "recreate-wechat-article" / "agents" / "openai.yaml",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
        self.assertIn("--image-mode rebuild|copy", combined)
        self.assertIn("默认 `copy`", combined)
        self.assertIn("默认采用 copy + ai", combined)
        self.assertIn("1122×1402", combined)
        self.assertIn("#F7F6F6", combined)
        self.assertIn("normalize_ai_card.py", combined)
        for retired_name in ("ri" + "ch", "pu" + "re"):
            self.assertNotIn(retired_name, combined)

    def test_new_article_and_comic_modes_are_documented(self):
        files = [
            ROOT / ".agents" / "skills" / "recreate-wechat-article" / "SKILL.md",
            ROOT / ".agents" / "skills" / "recreate-wechat-article" / "references" / "image-generation-modes.md",
            ROOT / ".agents" / "skills" / "recreate-wechat-article" / "agents" / "openai.yaml",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
        for value in ("article-mode", "trans", "ori", "comic", "comic-count", "generatedImageLink"):
            self.assertIn(value, combined)


class CardTests(unittest.TestCase):
    def test_generation_failure_uses_vertical_template_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            reference = temp / "reference.png"
            output = temp / "card.png"
            Image.new("RGB", (500, 500), "#f7c8c8").save(reference)
            used_fallback = compose_card.compose(
                temp / "missing-ai-image.png", reference, "温柔鼓励孩子前行", "喜", output
            )
            self.assertTrue(used_fallback)
            with Image.open(output) as image:
                self.assertEqual(image.size, (1080, 1440))
                self.assertEqual(image.format, "PNG")

    def test_twenty_characters_fill_background_and_render_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            reference = temp / "reference.png"
            illustration = temp / "illustration.png"
            first = temp / "first.png"
            second = temp / "second.png"
            Image.new("RGB", (500, 500), "#efcaca").save(reference)
            Image.new("RGB", (900, 700), "#dcead8").save(illustration)
            caption = "温柔鼓励让孩子勇敢走向属于自己的远方吧！"
            self.assertEqual(compose_card.normalized_caption(caption).__len__(), 20)
            compose_card.compose(illustration, reference, caption, "喜", first)
            compose_card.compose(illustration, reference, caption, "喜", second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with Image.open(first) as image:
                expected = (255, 243, 198)
                self.assertEqual(image.getpixel((0, 910)), expected)
                self.assertEqual(image.getpixel((1079, 910)), expected)
                self.assertEqual(image.getpixel((0, 1439)), expected)
                self.assertEqual(image.getpixel((1079, 1439)), expected)
                self.assertEqual(image.getpixel((106, 986)), expected)

    def test_twenty_one_visible_characters_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            reference = temp / "reference.png"
            Image.new("RGB", (500, 500), "#efcaca").save(reference)
            with self.assertRaisesRegex(ValueError, "最多 20 个可见字符"):
                compose_card.compose(None, reference, "温柔鼓励让孩子勇敢走向属于自己的更远方吧！", "喜", temp / "card.png")

    def test_local_card_can_be_composed_without_brand_reference_when_illustration_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            illustration = temp / "illustration.png"
            output = temp / "card.png"
            Image.new("RGB", (900, 700), "#dcead8").save(illustration)
            compose_card.compose(illustration, None, "不依赖固定人物", "通用", output)
            with Image.open(output) as image:
                self.assertEqual(image.size, (1080, 1440))
                self.assertEqual(image.format, "PNG")

    def test_art_font_size_is_scaled_to_one_and_a_half(self):
        self.assertEqual(compose_card.ART_FONT_SCALE, 1.5)
        self.assertEqual(compose_card.ART_FONT_SIZE_MAX, 138)
        self.assertEqual(compose_card.ART_FONT_SIZE_MIN, 78)

    def test_art_font_preference_is_available(self):
        font = compose_card.load_font(56, artistic=True)
        self.assertTrue(hasattr(font, "getbbox"))
        if hasattr(font, "path"):
            self.assertIn(Path(font.path).name.upper(), {name.upper() for name in compose_card.ART_FONT_NAMES})


class AiCardNormalizationTests(unittest.TestCase):
    def test_exact_size_background_and_compact_vertical_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            raw = temp / "raw.png"
            output = temp / "card.png"
            image = Image.new("RGB", (1024, 1536), "#FFFFFF")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((180, 220, 844, 1260), radius=48, fill="#E99A67")
            draw.rectangle((260, 1080, 760, 1220), fill="#222222")
            image.save(raw)

            result = normalize_ai_card.normalize(raw, output)

            self.assertEqual(result["outputSize"], [1122, 1402])
            self.assertEqual(result["background"], "#F7F6F6")
            with Image.open(output) as normalized:
                self.assertEqual(normalized.size, (1122, 1402))
                self.assertEqual(normalized.format, "PNG")
                for point in ((0, 0), (1121, 0), (0, 1401), (1121, 1401)):
                    self.assertEqual(normalized.getpixel(point), (247, 246, 246))
                background = Image.new("RGB", normalized.size, (247, 246, 246))
                bbox = ImageChops.difference(normalized.convert("RGB"), background).getbbox()
                self.assertIsNotNone(bbox)
                assert bbox is not None
                self.assertLess(bbox[1], 70)
                self.assertGreater(bbox[3], 1330)

    def test_rejects_non_portrait_ai_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            raw = temp / "raw.png"
            Image.new("RGB", (900, 600), "#F7F6F6").save(raw)
            with self.assertRaisesRegex(normalize_ai_card.NormalizeError, "必须为纵向"):
                normalize_ai_card.normalize(raw, temp / "card.png")


class ManifestBuilderTests(unittest.TestCase):
    def test_ori_comic_allows_generated_images_without_source_indexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source_image = temp / "source-images" / "image-001.png"
            comic_image = temp / "cards" / "comic-001.png"
            source_image.parent.mkdir()
            comic_image.parent.mkdir()
            Image.new("RGB", (600, 900), "#d8e4ef").save(source_image)
            Image.new("RGB", (1122, 1402), "#f7f6f6").save(comic_image)
            source = {
                "success": True,
                "sourceUrl": "https://mp.weixin.qq.com/s/comic",
                "title": "原文",
                "blocks": [
                    {"type": "text", "sourceIndex": 1, "text": "原文内容。"},
                    {"type": "image", "sourceIndex": 2, "url": "https://mmbiz.qpic.cn/a.jpg", "localPath": "source-images/image-001.png"},
                ],
            }
            article = {
                "articleMode": "ori", "imageMode": "comic", "comicCount": 2,
                "title": "漫画版原文",
                "blocks": [
                    {"type": "image", "path": "cards/comic-001.png", "alt": "第一幕"},
                    {"type": "generatedImageLink", "alt": "第二幕", "error": "生成失败"},
                ],
            }
            source_path = temp / "source.json"
            article_path = temp / "article-plan.json"
            manifest_path = temp / "manifest.json"
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
            build_manifest.write_manifest(source_path, article_path, manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["imageMode"], "comic")
            self.assertNotIn("sourceIndex", manifest["blocks"][0])
            rendered = render_article.render(manifest_path, temp / "result")
            self.assertEqual(rendered["imageCount"], 1)
            self.assertEqual(rendered["failedImageCount"], 1)
            validation = validate_output.validate(Path(rendered["html"]), (1122, 1402))
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(validation["comicCount"], 2)

    def test_comic_requires_exact_result_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = {
                "success": True, "sourceUrl": "https://mp.weixin.qq.com/s/comic",
                "title": "原文", "blocks": [{"type": "text", "sourceIndex": 1, "text": "原文。"}],
            }
            article = {
                "articleMode": "ori", "imageMode": "comic", "comicCount": 2,
                "title": "漫画", "blocks": [{"type": "generatedImageLink", "error": "失败"}],
            }
            source_path = temp / "source.json"
            article_path = temp / "article-plan.json"
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(build_manifest.BuildError, "comic 结果数量"):
                build_manifest.build(source_path, article_path)
    def test_image_only_source_builds_and_renders_with_zero_text_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source_image = temp / "source-images" / "image-001.png"
            source_image.parent.mkdir()
            Image.new("RGB", (500, 800), "#f7f6f6").save(source_image)
            source = {
                "success": True,
                "contentMode": "image_only",
                "sourceUrl": "https://mp.weixin.qq.com/s/image-only",
                "title": "图片正文",
                "blocks": [{
                    "type": "image", "sourceIndex": 0,
                    "url": "https://mmbiz.qpic.cn/image-only.png",
                    "localPath": "source-images/image-001.png",
                }],
            }
            article = {
                "title": "看懂图片里的生活提醒",
                "summary": "图片把情绪和信息放在同一张画面里。",
                "blocks": [
                    {"type": "text", "text": "先读画面，再读情绪。"},
                    {"type": "image", "sourceIndex": 0, "path": "cards/image-001.png"},
                ],
            }
            generated_image = temp / "cards" / "image-001.png"
            generated_image.parent.mkdir()
            Image.new("RGB", (1122, 1402), "#f7f6f6").save(generated_image)
            source_path = temp / "source.json"
            article_path = temp / "article-plan.json"
            manifest_path = temp / "manifest.json"
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")

            result = build_manifest.write_manifest(source_path, article_path, manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["sourceCharacterCount"], 0)
            self.assertEqual(result["imageCount"], 1)
            rendered = render_article.render(manifest_path, temp / "result")
            self.assertEqual(rendered["rewriteCharacterLimit"], 800)
            validation = validate_output.validate(Path(rendered["html"]), (1122, 1402))
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertTrue(Path(rendered["markdown"]).is_file())

    def test_builds_archive_count_and_mixed_image_outcomes_automatically(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source_image = temp / "source-images" / "image-001.jpg"
            generated_image = temp / "cards" / "image-001.png"
            source_image.parent.mkdir()
            generated_image.parent.mkdir()
            Image.new("RGB", (600, 900), "#d8e4ef").save(source_image)
            Image.new("RGB", (600, 900), "#ead8cf").save(generated_image)
            source = {
                "success": True,
                "sourceUrl": "https://mp.weixin.qq.com/s/example",
                "title": "原始标题",
                "blocks": [
                    {"type": "text", "sourceIndex": 1, "text": "保留的原文正文。" * 20},
                    {
                        "type": "image", "sourceIndex": 2,
                        "url": "https://mmbiz.qpic.cn/a.jpg", "localPath": "source-images/image-001.jpg",
                    },
                    {"type": "text", "sourceIndex": 3, "text": "广告推广内容"},
                    {"type": "image", "sourceIndex": 4, "url": "https://mmbiz.qpic.cn/b.jpg"},
                ],
            }
            article = {
                "title": "新的标题",
                "summary": "简短导语。",
                "removedSourceIndexes": [3],
                "blocks": [
                    {"type": "text", "text": "浓缩后的原创正文。"},
                    {"type": "image", "sourceIndex": 2, "path": "cards/image-001.png"},
                    {"type": "sourceImageLink", "sourceIndex": 4},
                ],
            }
            source_path = temp / "source.json"
            article_path = temp / "article-plan.json"
            manifest_path = temp / "manifest.json"
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")

            result = build_manifest.write_manifest(source_path, article_path, manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(result["imageCount"], 1)
            self.assertEqual(result["sourceImageLinkCount"], 1)
            self.assertEqual(manifest["sourceCharacterCount"], render_article.non_whitespace_count(source["blocks"][0]["text"]))
            self.assertEqual([block["sourceIndex"] for block in manifest["sourceArchive"]["blocks"]], [1, 2, 4])
            self.assertNotIn("广告推广内容", json.dumps(manifest, ensure_ascii=False))
            self.assertEqual(manifest["blocks"][-1]["url"], "https://mmbiz.qpic.cn/b.jpg")
            self.assertTrue(Path(manifest["blocks"][-2]["path"]).is_absolute())

            rendered = render_article.render(manifest_path, temp / "result")
            validation = validate_output.validate(Path(rendered["html"]))
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(rendered["imageCount"], 1)
            self.assertEqual(rendered["failedImageCount"], 1)
            self.assertTrue(Path(rendered["markdown"]).is_file())

    def test_requires_one_ordered_result_for_every_retained_source_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = {
                "success": True,
                "sourceUrl": "https://mp.weixin.qq.com/s/example",
                "title": "原始标题",
                "blocks": [
                    {"type": "text", "sourceIndex": 1, "text": "原始正文。" * 20},
                    {"type": "image", "sourceIndex": 2, "url": "https://mmbiz.qpic.cn/a.jpg"},
                    {"type": "image", "sourceIndex": 4, "url": "https://mmbiz.qpic.cn/b.jpg"},
                ],
            }
            article = {
                "title": "新的标题",
                "blocks": [
                    {"type": "text", "text": "原创正文。"},
                    {"type": "sourceImageLink", "sourceIndex": 4},
                    {"type": "sourceImageLink", "sourceIndex": 2},
                ],
            }
            source_path = temp / "source.json"
            article_path = temp / "article-plan.json"
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(build_manifest.BuildError, "相对顺序"):
                build_manifest.build(source_path, article_path)


class RenderTests(unittest.TestCase):
    def manifest(self, image_paths: list[Path]) -> dict:
        blocks = [
            {"type": "heading", "level": 2, "text": "新的起点"},
            {"type": "text", "text": "这是独立写作的第一段。\n这是第二段。"},
        ]
        for path in image_paths:
            blocks.append({"type": "image", "path": str(path), "alt": "新配图"})
        return {
            "title": "测试：标题/不能*覆盖?",
            "sourceUrl": "https://mp.weixin.qq.com/s/example",
            "summary": "新的导语。",
            "blocks": blocks,
        }

    def with_source_archive(self, manifest: dict, blocks: list[dict], title: str = "原始标题") -> dict:
        manifest["sourceCharacterCount"] = sum(
            render_article.non_whitespace_count(block["text"])
            for block in blocks
            if block["type"] == "text"
        )
        manifest["sourceArchive"] = {"title": title, "blocks": blocks}
        return manifest

    def test_no_image_and_duplicate_title_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(self.manifest([]), ensure_ascii=False), encoding="utf-8")
            now = datetime(2026, 8, 18, 14, 30, 5, tzinfo=render_article.SHANGHAI_TZ)
            first = render_article.render(manifest_path, temp / "result", now)
            second = render_article.render(manifest_path, temp / "result", now)
            self.assertNotEqual(first["html"], second["html"])
            self.assertTrue(Path(first["html"]).is_file())
            self.assertTrue(Path(second["html"]).is_file())
            self.assertNotRegex(Path(first["html"]).name, r'[:/*?]')
            self.assertEqual(Path(first["html"]).stem, Path(first["html"]).parent.name)
            self.assertEqual(Path(second["html"]).stem, Path(second["html"]).parent.name)
            self.assertTrue(validate_output.validate(Path(first["html"]))["valid"])

    def test_multiple_vertical_images_are_copied_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            images = []
            for index in range(3):
                path = temp / f"card-{index}.png"
                Image.new("RGB", (600, 900), (240, 220 - index * 20, 210)).save(path)
                images.append(path)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(self.manifest(images), ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            validation = validate_output.validate(Path(result["html"]))
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(result["imageCount"], 3)
            self.assertEqual(len(validation["images"]), 3)

    def test_validator_can_enforce_caption_mode_image_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            card = temp / "card.png"
            Image.new("RGB", (600, 900), "#ead8cf").save(card)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(self.manifest([card]), ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            validation = validate_output.validate(Path(result["html"]), (1122, 1402))
            self.assertFalse(validation["valid"])
            self.assertEqual(validation["expectedImageSize"], [1122, 1402])
            self.assertTrue(any("期望 1122x1402" in error for error in validation["errors"]))

    def test_failed_generated_image_renders_clickable_original_link_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            card = temp / "card.png"
            Image.new("RGB", (600, 900), "#ead8cf").save(card)
            source_blocks = [
                {"type": "text", "sourceIndex": 1, "text": "用于字符计数的原始正文。" * 20},
                {"type": "image", "sourceIndex": 2, "url": "https://mmbiz.qpic.cn/a.jpg"},
                {"type": "image", "sourceIndex": 4, "url": "https://mmbiz.qpic.cn/b.jpg"},
            ]
            manifest = self.with_source_archive(self.manifest([]), source_blocks)
            manifest["blocks"].extend([
                {"type": "image", "sourceIndex": 2, "path": str(card), "alt": "成功图片"},
                {"type": "sourceImageLink", "sourceIndex": 4, "url": "https://mmbiz.qpic.cn/b.jpg"},
            ])
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            result = render_article.render(manifest_path, temp / "result")
            document = Path(result["html"]).read_text(encoding="utf-8")
            validation = validate_output.validate(Path(result["html"]))

            self.assertEqual(result["imageCount"], 1)
            self.assertEqual(result["failedImageCount"], 1)
            self.assertIn("原始图片链接：", document)
            self.assertIn('href="https://mmbiz.qpic.cn/b.jpg"', document)
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(validation["mediaSequence"], ["image", "sourceImageLink"])
            self.assertEqual(validation["sourceImageLinks"][0]["url"], "https://mmbiz.qpic.cn/b.jpg")

    def test_failed_generated_image_without_url_is_marked_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            manifest = self.manifest([])
            manifest["blocks"].append({"type": "sourceImageLink", "sourceIndex": 2})
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            document = Path(result["html"]).read_text(encoding="utf-8")
            validation = validate_output.validate(Path(result["html"]))
            self.assertIn("原始图片链接：", document)
            self.assertIn("不可用", document)
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(validation["sourceImageLinks"], [{"url": None, "available": False}])

    def test_source_archive_is_interleaved_local_and_url_inside_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            card = temp / "card.png"
            source_image = temp / "source.jpg"
            Image.new("RGB", (600, 900), "#ebd8ca").save(card)
            Image.new("RGB", (640, 800), "#cad8eb").save(source_image)
            source_blocks = [
                {"type": "text", "sourceIndex": 1, "text": "这是清洗后保留的原文第一段。" * 5},
                {
                    "type": "image",
                    "sourceIndex": 2,
                    "url": "https://mmbiz.qpic.cn/example/640?wx_fmt=jpeg",
                    "path": str(source_image),
                    "alt": "原文配图",
                },
                {"type": "text", "sourceIndex": 3, "text": "这是清洗后保留的原文第二段。" * 5},
            ]
            manifest = self.with_source_archive(self.manifest([card]), source_blocks)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            result = render_article.render(manifest_path, temp / "result")
            package = Path(result["packageDir"])
            document = Path(result["html"]).read_text(encoding="utf-8")
            markdown = Path(result["markdown"]).read_text(encoding="utf-8")
            self.assertEqual(Path(result["html"]).parent, package)
            self.assertEqual(Path(result["html"]).stem, package.name)
            self.assertEqual(Path(result["assets"]).name, "images")
            self.assertIn('src="images/image-001.png"', document)
            self.assertTrue((package / "source-images" / "image-001.jpg").is_file())
            self.assertLess(markdown.index(source_blocks[0]["text"]), markdown.index("![原文配图]"))
            self.assertLess(markdown.index("![原文配图]"), markdown.index(source_blocks[2]["text"]))
            self.assertIn("原始图片链接：<https://mmbiz.qpic.cn/example/640?wx_fmt=jpeg>", markdown)
            self.assertNotIn("广告推广内容", markdown)
            validation = validate_output.validate(Path(result["html"]))
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(len(validation["sourceArchive"]["sourceImages"]), 1)
            self.assertEqual(result["archiveWarnings"], [])

    def test_missing_source_image_continues_with_url_and_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source_blocks = [
                {"type": "text", "sourceIndex": 1, "text": "用于归档计数的清洗后原文。" * 8},
                {
                    "type": "image",
                    "sourceIndex": 2,
                    "url": "https://mmbiz.qpic.cn/missing/640?wx_fmt=jpeg",
                    "path": str(temp / "missing.jpg"),
                },
            ]
            manifest = self.with_source_archive(self.manifest([]), source_blocks)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            markdown = Path(result["markdown"]).read_text(encoding="utf-8")
            self.assertIsNone(result["sourceImages"])
            self.assertFalse((Path(result["packageDir"]) / "source-images").exists())
            self.assertIn("> 原图 1 本地保存失败。", markdown)
            self.assertIn("https://mmbiz.qpic.cn/missing/640?wx_fmt=jpeg", markdown)
            self.assertEqual(len(result["archiveWarnings"]), 1)
            validation = validate_output.validate(Path(result["html"]))
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(validation["sourceArchive"]["failedImageCount"], 1)

    def test_validator_rejects_remote_source_image_embedding(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source_image = temp / "source.jpg"
            Image.new("RGB", (640, 800), "#cad8eb").save(source_image)
            source_blocks = [
                {"type": "text", "sourceIndex": 1, "text": "用于验证本地归档的原文。" * 10},
                {
                    "type": "image",
                    "sourceIndex": 2,
                    "url": "https://mmbiz.qpic.cn/example/640?wx_fmt=jpeg",
                    "path": str(source_image),
                },
            ]
            manifest = self.with_source_archive(self.manifest([]), source_blocks)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            markdown_path = Path(result["markdown"])
            markdown = markdown_path.read_text(encoding="utf-8").replace(
                "source-images/image-001.jpg",
                "https://mmbiz.qpic.cn/example/640?wx_fmt=jpeg",
            )
            markdown_path.write_text(markdown, encoding="utf-8")
            validation = validate_output.validate(Path(result["html"]))
            self.assertFalse(validation["valid"])
            self.assertTrue(any("原图不是 source-images/" in error for error in validation["errors"]))

    def test_text_only_source_archive_has_markdown_without_source_image_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source_blocks = [{"type": "text", "sourceIndex": 1, "text": "只有文字的清洗后原文。" * 10}]
            manifest = self.with_source_archive(self.manifest([]), source_blocks)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            self.assertTrue(Path(result["markdown"]).is_file())
            self.assertFalse((Path(result["packageDir"]) / "source-images").exists())
            self.assertTrue(validate_output.validate(Path(result["html"]))["valid"])

    def test_source_character_count_must_match_archived_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            manifest = self.with_source_archive(
                self.manifest([]),
                [{"type": "text", "sourceIndex": 1, "text": "真实归档正文" * 20}],
            )
            manifest["sourceCharacterCount"] += 1
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(render_article.ManifestError, "sourceArchive 正文实际"):
                render_article.render(manifest_path, temp / "result")

    def test_source_archive_blocks_must_keep_source_index_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source_blocks = [
                {"type": "text", "sourceIndex": 2, "text": "原文第二块。" * 12},
                {"type": "text", "sourceIndex": 1, "text": "原文第一块。" * 12},
            ]
            manifest = self.with_source_archive(self.manifest([]), source_blocks)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(render_article.ManifestError, "sourceIndex 严格递增"):
                render_article.render(manifest_path, temp / "result")

    def test_publish_failure_removes_staging_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            card = temp / "card.png"
            Image.new("RGB", (600, 900), "#ebd8ca").save(card)
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(self.manifest([card]), ensure_ascii=False), encoding="utf-8")
            result_dir = temp / "result"
            with mock.patch.object(render_article.shutil, "copyfile", side_effect=OSError("copy failed")):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    render_article.render(manifest_path, result_dir)
            self.assertEqual(list(result_dir.iterdir()), [])

    def test_punctuation_lines_keep_delimiters_and_preserve_numbers(self):
        text = "第一句，第二句。真的吗？！数字3.14不拆，数量1,000也不拆;tail"
        self.assertEqual(
            render_article.split_punctuation_lines(text),
            ["第一句，", "第二句。", "真的吗？！", "数字3.14不拆，", "数量1,000也不拆;", "tail"],
        )

    def test_all_requested_line_endings_and_newline(self):
        text = "甲，乙。丙！丁？戊；己,庚.辛!壬;癸?\n无标点尾行"
        self.assertEqual(
            render_article.split_punctuation_lines(text),
            ["甲，", "乙。", "丙！", "丁？", "戊；", "己,", "庚.", "辛!", "壬;", "癸?", "无标点尾行"],
        )

    def test_rewrite_limit_and_legacy_manifest_compatibility(self):
        self.assertEqual(render_article.rewrite_character_limit(1000), 700)
        self.assertEqual(render_article.rewrite_character_limit(2000), 800)
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            manifest_path = temp / "manifest.json"
            legacy = self.manifest([])
            self.assertNotIn("sourceCharacterCount", legacy)
            manifest_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            self.assertIsNone(result["sourceCharacterCount"])
            self.assertIsNone(result["rewriteCharacterLimit"])
            self.assertIsNone(result["markdown"])
            self.assertFalse((Path(result["packageDir"]) / "source.md").exists())

    def test_manifest_rejects_rewrite_over_seventy_percent(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            manifest = {
                "title": "过长测试",
                "sourceUrl": "https://mp.weixin.qq.com/s/example",
                "sourceCharacterCount": 10,
                "blocks": [{"type": "text", "text": "一二三四五六七八"}],
            }
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(render_article.ManifestError, "超过允许上限 7"):
                render_article.render(manifest_path, temp / "result")

    def test_ori_skips_rewrite_length_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            manifest = {
                "title": "原文直出", "sourceUrl": "https://mp.weixin.qq.com/s/example",
                "articleMode": "ori", "imageMode": "copy", "sourceCharacterCount": 10,
                "blocks": [{"type": "text", "text": "一二三四五六七八"}],
            }
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            self.assertIsNone(result["rewriteCharacterLimit"])

    def test_rendered_html_uses_centered_sentence_lines_on_mobile(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            manifest = self.manifest([])
            manifest["sourceCharacterCount"] = 200
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = render_article.render(manifest_path, temp / "result")
            document = Path(result["html"]).read_text(encoding="utf-8")
            self.assertIn('<span class="sentence-line">这是独立写作的第一段。</span>', document)
            self.assertIn(".sentence-line { display:block; text-align:center; }", document)
            self.assertIn("p { font-size:17px; text-align:center; }", document)
            self.assertNotIn("text-align:justify", document)
            self.assertNotIn("text-align:left", document)


if __name__ == "__main__":
    unittest.main()
