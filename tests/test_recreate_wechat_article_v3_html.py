from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents" / "skills" / "recreate-wechat-article-v3" / "scripts"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块：{filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_article = load_module("v3_render_article", "render_article.py")
validate_output = load_module("v3_validate_output", "validate_output.py")


class RecreateWechatArticleV3HtmlTests(unittest.TestCase):
    def test_renderer_uses_flat_inline_markup_for_all_block_types(self) -> None:
        data = {
            "title": "测试文章",
            "sourceUrl": "https://mp.weixin.qq.com/s/test",
            "articleMode": "ori",
            "imageMode": "copy",
            "summary": "导语，结尾。",
            "blocks": [
                {"type": "heading", "level": 2, "text": "章节标题"},
                {"type": "text", "text": "甲，乙\n丙。"},
                {"type": "quote", "text": "引用，结束。"},
                {"type": "image", "alt": "配图"},
                {"type": "sourceImageLink", "sourceIndex": 3, "url": "https://img.example/a.png"},
                {"type": "sourceImageLink", "sourceIndex": 4},
                {"type": "generatedImageLink", "error": "生成失败"},
            ],
        }

        document, image_count, failed_count = render_article.build_html(
            data, "images", "2026-09-24T12:00:00+08:00"
        )

        self.assertEqual((image_count, failed_count), (1, 3))
        for forbidden in ("<style", "class=", "<figure"):
            self.assertNotIn(forbidden, document)
        for line in ("导语，", "结尾。", "甲，", "乙", "丙。", "引用，", "结束。"):
            self.assertIn(
                f'<p style="text-align:center;"><span style="text-wrap-mode: wrap;">{line}</span></p>',
                document,
            )
        self.assertIn('<p style="text-align:center;"><span style="text-wrap-mode: wrap;"><br/></span></p>', document)
        self.assertIn('style="text-align:center;"', document)
        self.assertNotIn('<p style="margin:', document)
        self.assertIn("<body>", document)
        self.assertIn("<main>", document)
        self.assertNotIn("<body style=", document)
        self.assertNotIn("<main style=", document)
        self.assertIn('<img src="images/image-001.png"', document)
        self.assertIn('data-source-image-link="available"', document)
        self.assertIn('data-source-image-link="unavailable"', document)
        self.assertIn('data-generated-image-link="true"', document)

        parser = validate_output.ArticleParser()
        parser.feed(document)
        self.assertEqual(parser.source_image_links, ["https://img.example/a.png", None])
        self.assertEqual(parser.generated_image_links, 1)

    def test_validator_accepts_flat_document_and_rejects_legacy_markup(self) -> None:
        data = {
            "title": "测试文章",
            "sourceUrl": "https://mp.weixin.qq.com/s/test",
            "articleMode": "ori",
            "imageMode": "comic",
            "comicCount": 2,
            "blocks": [
                {"type": "text", "text": "正文。"},
                {"type": "image", "alt": "配图"},
                {"type": "generatedImageLink", "error": "生成失败"},
            ],
        }
        document, _, _ = render_article.build_html(data, "images", "2026-09-24T12:00:00+08:00")

        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "0924-测试文章"
            package.mkdir()
            html_path = package / "0924-测试文章.html"
            images = package / "images"
            images.mkdir()
            Image.new("RGB", (1080, 1350), "white").save(images / "image-001.png")
            html_path.write_text(document, encoding="utf-8")

            valid = validate_output.validate(html_path, (1080, 1350))
            self.assertTrue(valid["valid"], valid["errors"])
            self.assertEqual(valid["mediaSequence"], ["image", "generatedImageLink"])

            bad_paragraph = document.replace(
            '<p style="text-align:center;"><span style="text-wrap-mode: wrap;">正文。</span></p>',
                '<p style="margin:0"><span style="text-wrap-mode: wrap;">正文。</span></p>',
                1,
            )
            html_path.write_text(bad_paragraph, encoding="utf-8")
            invalid_paragraph = validate_output.validate(html_path, (1080, 1350))
            self.assertFalse(invalid_paragraph["valid"])
            self.assertIn("p 标签只能使用 text-align:center; 样式", "\n".join(invalid_paragraph["errors"]))

            bad_span = document.replace("text-wrap-mode: wrap;", "overflow-wrap:anywhere;", 1)
            html_path.write_text(bad_span, encoding="utf-8")
            invalid_span = validate_output.validate(html_path, (1080, 1350))
            self.assertFalse(invalid_span["valid"])
            self.assertIn("span 只能使用", "\n".join(invalid_span["errors"]))

            html_path.write_text(document.replace("<article>", '<article class="legacy"><style></style><figure><span>x</span></figure>'), encoding="utf-8")
            invalid = validate_output.validate(html_path, (1080, 1350))
            self.assertFalse(invalid["valid"])
            self.assertTrue(any("不允许使用标签" in error for error in invalid["errors"]))
            self.assertIn("HTML 不允许使用 class 属性。", invalid["errors"])


if __name__ == "__main__":
    unittest.main()
