from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V3_SCRIPTS = ROOT / ".agents" / "skills" / "recreate-wechat-article-v3" / "scripts"
WEB_ROOT = ROOT / "web-v3"
sys.path.insert(0, str(WEB_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v3_build = load_module("nearby_v3_build_manifest", V3_SCRIPTS / "build_manifest.py")
v3_render = load_module("nearby_v3_render_article", V3_SCRIPTS / "render_article.py")
web_build = load_module("nearby_web_build_manifest", WEB_ROOT / "scripts" / "build_manifest.py")
web_render = load_module("nearby_web_render_article", WEB_ROOT / "scripts" / "render_article.py")
import website_pipeline  # noqa: E402


class NearbyManifestTests(unittest.TestCase):
    def make_inputs(self, root: Path, article: dict) -> tuple[Path, Path]:
        source = {
            "success": True,
            "sourceUrl": "https://mp.weixin.qq.com/s/nearby",
            "title": "原标题",
            "blocks": [
                {"type": "text", "sourceIndex": 0, "text": "第一段原文。"},
                {"type": "image", "sourceIndex": 1, "url": "https://img.example/1.jpg"},
                {"type": "text", "sourceIndex": 2, "text": "第二段原文。"},
            ],
        }
        source_path = root / "source.json"
        article_path = root / "article-plan.json"
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
        return source_path, article_path

    def nearby_article(self, root: Path) -> dict:
        image = root / "card.png"
        image.write_bytes(b"png")
        return {
            "articleMode": "nearby",
            "imageMode": "copy",
            "title": "模型错误标题",
            "blocks": [
                {"type": "text", "text": "第一段改写。"},
                {"type": "image", "sourceIndex": 1, "path": str(image)},
                {"type": "text", "text": "第二段改写。"},
            ],
        }

    def test_native_and_web_manifests_use_source_title_and_allow_nearby_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = self.nearby_article(root)
            source_path, article_path = self.make_inputs(root, article)
            for builder in (v3_build, web_build):
                with self.subTest(builder=builder.__name__):
                    manifest, _ = builder.build(source_path, article_path)
                    self.assertEqual(manifest["title"], "原标题")
                    self.assertEqual(manifest["articleMode"], "nearby")
                    self.assertNotIn("summary", manifest)

    def test_nearby_rejects_summary_and_structure_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for builder in (v3_build, web_build):
                with self.subTest(builder=builder.__name__, case="summary"):
                    article = self.nearby_article(root)
                    article["summary"] = "不应存在的导语"
                    source_path, article_path = self.make_inputs(root, article)
                    with self.assertRaises(ValueError):
                        builder.build(source_path, article_path)

                with self.subTest(builder=builder.__name__, case="heading"):
                    article = self.nearby_article(root)
                    article["blocks"][0] = {"type": "heading", "level": 2, "text": "新增章节"}
                    source_path, article_path = self.make_inputs(root, article)
                    with self.assertRaises(ValueError):
                        builder.build(source_path, article_path)

    def test_native_renderer_does_not_apply_trans_limit_to_nearby(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "title": "原标题",
                "sourceUrl": "https://mp.weixin.qq.com/s/nearby",
                "articleMode": "nearby",
                "imageMode": "copy",
                "sourceCharacterCount": 10,
                "blocks": [{"type": "text", "text": "改" * 801}],
            }, ensure_ascii=False), encoding="utf-8")
            result = v3_render.render(manifest_path, root / "result")
            self.assertIsNone(result["rewriteCharacterLimit"])
            self.assertIn('meta name="article-mode" content="nearby"', Path(result["html"]).read_text(encoding="utf-8"))

    def test_web_renderer_does_not_apply_trans_limit_to_nearby(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "title": "原标题",
                "sourceUrl": "https://mp.weixin.qq.com/s/nearby",
                "articleMode": "nearby",
                "imageMode": "copy",
                "sourceCharacterCount": 10,
                "blocks": [{"type": "text", "text": "改" * 801}],
            }, ensure_ascii=False), encoding="utf-8")
            result = web_render.render(manifest_path, root / "result")
            self.assertIsNone(result["rewriteCharacterLimit"])
            self.assertIn('meta name="article-mode" content="nearby"', Path(result["html"]).read_text(encoding="utf-8"))


class NearbyPipelineTests(unittest.TestCase):
    def test_normalizer_drops_summary_and_requires_source_block_shape(self) -> None:
        source = {
            "title": "原标题",
            "blocks": [
                {"type": "text", "sourceIndex": 0, "text": "第一段。"},
                {"type": "text", "sourceIndex": 1, "text": "第二段。"},
            ],
        }
        messages: list[str] = []
        normalized = website_pipeline._normalize_article_plan(
            source,
            {
                "title": "错误标题",
                "summary": "不应保留",
                "removedSourceIndexes": [],
                "blocks": [
                    {"type": "text", "text": "第一段改写。"},
                    {"type": "text", "text": "第二段改写。"},
                ],
            },
            "nearby", "copy", messages.append,
        )
        self.assertEqual(normalized["title"], "原标题")
        self.assertNotIn("summary", normalized)
        self.assertTrue(messages)

        with self.assertRaises(website_pipeline.PipelineError):
            website_pipeline._normalize_article_plan(
                source,
                {"blocks": [{"type": "heading", "text": "新章节"}, {"type": "text", "text": "第二段改写。"}]},
                "nearby", "copy", lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
