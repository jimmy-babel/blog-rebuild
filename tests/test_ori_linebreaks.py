from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "new-skill": ROOT / ".agents" / "skills" / "recreate-wechat-article-new" / "scripts",
    "web-v2": ROOT / "web-v2" / "scripts",
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OriLineBreakTests(unittest.TestCase):
    def test_fetch_preserves_explicit_html_line_boundaries(self):
        source = """
        <html><head><meta property="og:title" content="测试标题"></head><body>
          <div id="js_content">
            <p>01</p>
            <p>第一行</p>
            <p>第二行<br>第三行</p>
            <div><img data-src="https://img.example/a.jpg"></div>
            <p>第四行</p>
          </div>
        </body></html>
        """
        expected_types = ["text", "image", "text"]
        expected_text = "01\n第一行\n第二行\n第三行"
        for label, scripts in MODULES.items():
            with self.subTest(parser=label):
                fetch = load_module(f"{label}-fetch", scripts / "fetch_article.py")
                article = fetch.parse_article("https://mp.weixin.qq.com/s/x", source)
                self.assertEqual([block["type"] for block in article["blocks"]], expected_types)
                self.assertEqual(article["blocks"][0]["text"], expected_text)
                self.assertEqual(article["blocks"][1]["sourceIndex"], 1)
                self.assertEqual(article["blocks"][2]["text"], "第四行")
                self.assertNotIn("\n\n", article["blocks"][0]["text"])

    def test_ori_renders_source_lines_in_one_continuous_paragraph(self):
        for label, scripts in MODULES.items():
            with self.subTest(renderer=label):
                render = load_module(f"{label}-render", scripts / "render_article.py")
                data = {
                    "title": "测试文章",
                    "sourceUrl": "https://mp.weixin.qq.com/s/x",
                    "articleMode": "ori",
                    "imageMode": "copy",
                    "blocks": [{"type": "text", "text": "甲\n乙\n丙"}],
                }
                document, _, _ = render.build_html(data, "images", "2026-09-14T00:00:00+08:00")
                self.assertEqual(document.count("<p>"), 1)
                self.assertEqual(document.count('class="sentence-line"'), 3)
                self.assertIn(
                    '<span class="sentence-line">甲</span><span class="sentence-line">乙</span><span class="sentence-line">丙</span>',
                    document,
                )

    def test_ori_preserves_newlines_and_punctuation_boundaries_together(self):
        for label, scripts in MODULES.items():
            with self.subTest(renderer=label):
                render = load_module(f"{label}-render-mixed", scripts / "render_article.py")
                data = {
                    "title": "测试文章",
                    "sourceUrl": "https://mp.weixin.qq.com/s/x",
                    "articleMode": "ori",
                    "imageMode": "copy",
                    "blocks": [{"type": "text", "text": "甲，乙\n丙。丁"}],
                }
                document, _, _ = render.build_html(data, "images", "2026-09-14T00:00:00+08:00")
                self.assertEqual(document.count("<p>"), 1)
                self.assertEqual(document.count('class="sentence-line"'), 4)
                for line in ("甲，", "乙", "丙。", "丁"):
                    self.assertIn(f'<span class="sentence-line">{line}</span>', document)

    def test_trans_keeps_existing_newline_paragraph_behavior(self):
        for label, scripts in MODULES.items():
            with self.subTest(renderer=label):
                render = load_module(f"{label}-render-trans", scripts / "render_article.py")
                data = {
                    "title": "测试文章",
                    "sourceUrl": "https://mp.weixin.qq.com/s/x",
                    "articleMode": "trans",
                    "imageMode": "copy",
                    "blocks": [{"type": "text", "text": "甲\n乙"}],
                }
                document, _, _ = render.build_html(data, "images", "2026-09-14T00:00:00+08:00")
                self.assertEqual(document.count("<p>"), 2)


if __name__ == "__main__":
    unittest.main()
