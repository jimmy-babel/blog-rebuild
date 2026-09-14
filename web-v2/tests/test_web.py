from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import app as app_module  # noqa: E402
import build_manifest  # noqa: E402
import normalize_ai_card  # noqa: E402
import website_pipeline as pipeline  # noqa: E402


def load_fetch_module():
    spec = importlib.util.spec_from_file_location("v2_fetch_article", ROOT / "scripts" / "fetch_article.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fetch_article = load_fetch_module()


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)
        self.client.__enter__()
        app_module.DB.init()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_health_is_direct_fetch(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["article_fetch"], "direct")
        self.assertNotIn("n8n_webhook", response.json())

    def test_url_and_comic_mode_validation(self) -> None:
        self.assertEqual(self.client.post("/api/tasks", json={"url": "https://example.com/a"}).status_code, 422)
        response = self.client.post("/api/tasks", json={"url": "https://mp.weixin.qq.com/s/x", "image_mode": "comic"})
        self.assertEqual(response.status_code, 422)

    def test_non_comic_ignores_invalid_irrelevant_comic_count(self) -> None:
        with patch.object(app_module, "schedule"):
            response = self.client.post("/api/tasks", json={
                "url": "https://mp.weixin.qq.com/s/copy",
                "image_mode": "copy",
                "comic_count": 0,
            })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["comic_count"], 5)
        app_module.DB.delete(response.json()["id"])

    def test_comic_rejects_non_positive_comic_count(self) -> None:
        response = self.client.post("/api/tasks", json={
            "url": "https://mp.weixin.qq.com/s/comic",
            "article_mode": "ori",
            "image_mode": "comic",
            "comic_count": 0,
        })
        self.assertEqual(response.status_code, 422)

    def test_create_full_mode_payload(self) -> None:
        with patch.object(app_module, "schedule"):
            response = self.client.post("/api/tasks", json={
                "url": "https://mp.weixin.qq.com/s/comic",
                "article_mode": "ori", "image_mode": "comic", "comic_count": 7, "brand": "off",
            })
        self.assertEqual(response.status_code, 202)
        task = response.json()
        self.assertEqual(task["article_mode"], "ori")
        self.assertEqual(task["image_mode"], "comic")
        self.assertEqual(task["comic_count"], 7)
        self.assertEqual(task["caption_mode"], "ai")
        self.assertEqual(task["brand_reference"], "off")
        app_module.DB.delete(task["id"])

    def test_brand_alias_conflict_is_rejected(self) -> None:
        response = self.client.post("/api/tasks", json={"url": "https://mp.weixin.qq.com/s/x", "brand": "on", "brand_reference": "off"})
        self.assertEqual(response.status_code, 422)

    def test_result_route_serves_html_and_relative_image(self) -> None:
        task_id = "result-route-v2"
        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            (result_dir / "images").mkdir()
            (result_dir / "article.html").write_text('<!doctype html><img src="images/image-001.png">', encoding="utf-8")
            (result_dir / "images" / "image-001.png").write_bytes(b"png")
            app_module.DB.create(task_id, "https://mp.weixin.qq.com/s/result", "trans", "copy", 5, "ai", "on")
            app_module.DB.update(task_id, status="completed", result_dir=str(result_dir), html_file=str(result_dir / "article.html"))
            self.assertEqual(self.client.get(f"/results/{task_id}/article.html").status_code, 200)
            self.assertEqual(self.client.get(f"/results/{task_id}/images/image-001.png").status_code, 200)
            app_module.DB.delete(task_id)


class DirectFetchTests(unittest.TestCase):
    def test_parser_extracts_text_images_and_source_indexes(self) -> None:
        source = '<html><head><meta property="og:title" content="测试标题"></head><body><div id="js_content"><p>第一段。</p><div><img data-src="https://img.example/a.jpg"></div><p>第二段。</p></div></body></html>'
        article = fetch_article.parse_article("https://mp.weixin.qq.com/s/x", source)
        self.assertEqual(article["title"], "测试标题")
        self.assertEqual([block["type"] for block in article["blocks"]], ["text", "image", "text"])
        self.assertEqual([block["sourceIndex"] for block in article["blocks"]], [0, 1, 2])
        self.assertEqual(article["blocks"][1]["url"], "https://img.example/a.jpg")

    def test_parser_uses_src_when_data_src_missing(self) -> None:
        article = fetch_article.parse_article("https://mp.weixin.qq.com/s/x", '<div id="js_content"><img src="/a.png"></div>')
        self.assertEqual(article["blocks"][0]["url"], "https://mp.weixin.qq.com/a.png")

    def test_fetch_failure_rejects_non_wechat_url(self) -> None:
        with self.assertRaises(fetch_article.FetchFailure):
            fetch_article.validate_wechat_url("https://example.com/article")


class DatabaseMigrationTests(unittest.TestCase):
    def test_legacy_table_gets_new_columns(self) -> None:
        from database import TaskStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, url TEXT, image_mode TEXT, caption_mode TEXT, brand_reference TEXT, status TEXT, created_at TEXT)")
                connection.commit()
            finally:
                connection.close()
            store = TaskStore(path)
            connection = store.connect()
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            finally:
                connection.close()
            self.assertTrue({"article_mode", "comic_count", "brand_reference"}.issubset(columns))


class PipelineTests(unittest.TestCase):
    def _write_manifest_inputs(self, root: Path, article: dict, source: dict | None = None) -> tuple[Path, Path]:
        source_path = root / "source.json"
        article_path = root / "article-plan.json"
        source_path.write_text(json.dumps(source or {
            "success": True,
            "sourceUrl": "https://mp.weixin.qq.com/s/test",
            "title": "来源标题",
            "blocks": [
                {"type": "text", "sourceIndex": 0, "text": "来源正文"},
                {"type": "image", "sourceIndex": 1, "url": "https://img.example/1.jpg"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
        return source_path, article_path

    def test_optional_plan_fields_are_repaired_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path, article_path = self._write_manifest_inputs(root, {
                "title": "",
                "summary": "",
                "removedSourceIndexes": [1, 1, "bad", 999],
                "comicCount": 0,
                "articleMode": "ori",
                "imageMode": "copy",
                "blocks": [
                    {"type": "heading", "level": 9, "text": "章节"},
                    {"type": "text", "text": ""},
                    {"type": "future-block", "text": "忽略"},
                ],
            })

            manifest, warnings = build_manifest.build(source_path, article_path)

            self.assertEqual(manifest["title"], "来源标题")
            self.assertNotIn("summary", manifest)
            self.assertEqual(manifest["blocks"], [{"type": "heading", "text": "章节", "level": 2}])
            self.assertTrue(any("summary 为空" in warning for warning in warnings))
            self.assertTrue(any("comicCount" in warning for warning in warnings))
            self.assertTrue(any("重复" in warning for warning in warnings))
            self.assertTrue(any("不受支持" in warning for warning in warnings))

    def test_missing_image_file_degrades_to_source_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path, article_path = self._write_manifest_inputs(root, {
                "title": "测试",
                "articleMode": "ori",
                "imageMode": "copy",
                "blocks": [
                    {"type": "text", "text": "正文"},
                    {"type": "image", "sourceIndex": 1, "path": str(root / "missing.png")},
                ],
            })

            manifest, warnings = build_manifest.build(source_path, article_path)

            self.assertEqual(manifest["blocks"][-1]["type"], "sourceImageLink")
            self.assertEqual(manifest["blocks"][-1]["url"], "https://img.example/1.jpg")
            self.assertTrue(any("降级为原图链接" in warning for warning in warnings))

    def test_pipeline_plan_normalization_falls_back_to_source_title(self) -> None:
        source = {
            "title": "原文标题",
            "blocks": [
                {"type": "text", "sourceIndex": 0, "text": "正文"},
                {"type": "image", "sourceIndex": 1, "url": "https://img.example/1.jpg"},
            ],
        }
        messages: list[str] = []
        normalized = pipeline._normalize_article_plan(
            source,
            {"title": "", "summary": None, "removedSourceIndexes": None, "blocks": [
                {"type": "text", "text": ""},
                {"type": "sourceImageLink", "sourceIndex": 1},
            ]},
            "ori", "copy", messages.append,
        )

        self.assertEqual(normalized["title"], "原文标题")
        self.assertNotIn("summary", normalized)
        self.assertEqual(normalized["removedSourceIndexes"], [])
        self.assertEqual(normalized["blocks"][0]["type"], "sourceImageLink")
        self.assertTrue(messages)

    def test_normalize_accepts_common_formats_and_ratios(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for extension, format_name, size in (("jpg", "JPEG", (1600, 900)), ("webp", "WEBP", (900, 900))):
                source = root / f"input.{extension}"
                output = root / f"output-{extension}.png"
                image = normalize_ai_card.Image.new("RGB", size, (230, 230, 230))
                image.save(source, format=format_name)

                normalize_ai_card.normalize(source, output)

                with normalize_ai_card.Image.open(output) as checked:
                    self.assertEqual(checked.size, normalize_ai_card.TARGET_SIZE)
                    self.assertEqual(checked.format, "PNG")

    def test_soft_rewrite_limit_is_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.json"
            article_path = root / "article-plan.json"
            source_path.write_text(json.dumps({
                "success": True,
                "sourceUrl": "https://mp.weixin.qq.com/s/test",
                "title": "来源标题",
                "blocks": [{"type": "text", "sourceIndex": 0, "text": "源" * 205}],
            }, ensure_ascii=False), encoding="utf-8")
            article_path.write_text(json.dumps({
                "title": "测试标题",
                "articleMode": "trans",
                "imageMode": "copy",
                "blocks": [{"type": "text", "text": "改" * 282}],
            }, ensure_ascii=False), encoding="utf-8")

            manifest, warnings = build_manifest.build(source_path, article_path)

            self.assertEqual(manifest["sourceCharacterCount"], 205)
            self.assertTrue(any("超过建议值 144" in warning for warning in warnings))
            self.assertEqual(manifest["warnings"], warnings)

    def test_build_allows_rewrite_over_800_characters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.json"
            article_path = root / "article-plan.json"
            source_path.write_text(json.dumps({
                "success": True,
                "sourceUrl": "https://mp.weixin.qq.com/s/test",
                "title": "来源标题",
                "blocks": [{"type": "text", "sourceIndex": 0, "text": "源" * 205}],
            }, ensure_ascii=False), encoding="utf-8")
            article_path.write_text(json.dumps({
                "title": "测试标题",
                "articleMode": "trans",
                "imageMode": "copy",
                "blocks": [{"type": "text", "text": "改" * 801}],
            }, ensure_ascii=False), encoding="utf-8")

            manifest, warnings = build_manifest.build(source_path, article_path)

            self.assertEqual(len(manifest["blocks"][0]["text"]), 801)
            self.assertTrue(any("超过建议值 144" in warning for warning in warnings))

    def test_extract_json_repairs_common_model_syntax_errors(self) -> None:
        result = pipeline._extract_json(
            '{"title":"测试","blocks":[{"type":"text","text":"她说"新买的？""}],}'
        )
        self.assertEqual(result["title"], "测试")
        self.assertEqual(result["blocks"][0]["text"], '她说"新买的？"')

    def test_extract_json_accepts_literal_newlines_in_string_values(self) -> None:
        result = pipeline._extract_json('{"title":"测试","blocks":[{"text":"第一行\n第二行"}]}')
        self.assertEqual(result["blocks"][0]["text"], "第一行\n第二行")

    def test_missing_source_images_become_ordered_fallback_links(self) -> None:
        source_blocks = [
            {"type": "text", "sourceIndex": 0, "text": "第一段"},
            {"type": "image", "sourceIndex": 1, "url": "https://img.example/1.jpg"},
            {"type": "text", "sourceIndex": 2, "text": "第二段"},
            {"type": "image", "sourceIndex": 3, "url": "https://img.example/3.jpg"},
            {"type": "text", "sourceIndex": 4, "text": "第三段"},
            {"type": "image", "sourceIndex": 5, "url": "https://img.example/5.jpg"},
        ]
        source_images = {block["sourceIndex"]: block for block in source_blocks if block["type"] == "image"}
        article_blocks = [
            {"type": "text", "text": "第一段"},
            {"type": "text", "text": "第二段"},
            {"type": "text", "text": "第三段"},
        ]

        repaired = pipeline._repair_missing_source_image_links(
            article_blocks, source_blocks, source_images, {1, 3, 5}
        )

        self.assertEqual(
            [(block["type"], block.get("sourceIndex")) for block in repaired],
            [("text", None), ("sourceImageLink", 1), ("text", None), ("sourceImageLink", 3),
             ("text", None), ("sourceImageLink", 5)],
        )
        self.assertEqual(
            [block["url"] for block in repaired if block["type"] == "sourceImageLink"],
            ["https://img.example/1.jpg", "https://img.example/3.jpg", "https://img.example/5.jpg"],
        )

    def test_planner_source_image_links_become_generation_jobs(self) -> None:
        source_images = {
            1: {"type": "image", "sourceIndex": 1, "url": "https://img.example/1.jpg"},
            3: {"type": "image", "sourceIndex": 3, "url": "https://img.example/3.jpg"},
        }
        blocks = [
            {"type": "sourceImageLink", "sourceIndex": 1, "imagePrompt": "朋友场景"},
            {"type": "sourceImageLink", "sourceIndex": 3, "alt": "家庭场景"},
        ]

        normalized = pipeline._promote_source_image_links_to_jobs(blocks, source_images, set())

        self.assertEqual([block["type"] for block in normalized], ["image", "image"])
        self.assertEqual(normalized[0]["imagePrompt"], "朋友场景")
        self.assertIn("家庭场景", normalized[1]["imagePrompt"])

    def test_missing_source_images_are_promoted_to_generation_jobs(self) -> None:
        source_blocks = [
            {"type": "text", "sourceIndex": 0, "text": "第一段"},
            {"type": "image", "sourceIndex": 1, "url": "https://img.example/1.jpg"},
        ]
        source_images = {1: source_blocks[1]}
        repaired = pipeline._repair_missing_source_image_links(
            [{"type": "text", "text": "第一段"}], source_blocks, source_images, {1}
        )

        normalized = pipeline._promote_source_image_links_to_jobs(repaired, source_images, set())

        self.assertEqual(normalized[1]["type"], "image")
        self.assertNotIn("error", normalized[1])
        self.assertTrue(normalized[1]["imagePrompt"])

    def test_eof_failure_detection_covers_cpa_body_and_transport_error(self) -> None:
        self.assertTrue(pipeline._is_eof_failure(pipeline.PipelineError("HTTP 500；响应 body：unexpected EOF")))
        self.assertTrue(pipeline._is_eof_failure(pipeline.httpx.ReadError("peer closed connection")))
        self.assertTrue(pipeline._is_eof_failure(pipeline.httpx.RemoteProtocolError("Server disconnected without sending a response")))
        self.assertFalse(pipeline._is_eof_failure(pipeline.PipelineError("HTTP 500；响应 body：内部服务错误")))
        self.assertFalse(pipeline._is_eof_failure(pipeline.httpx.ReadTimeout("timed out")))

    def test_eof_retries_once_after_ten_seconds(self) -> None:
        class FakeAI:
            def __init__(self):
                self.attempts = []

            def generate(self, prompt, references, output, request_dir=None, attempt=1):
                self.attempts.append(attempt)
                if attempt == 1:
                    raise pipeline.PipelineError("图片生成 API 调用失败：HTTP 500；响应 body：unexpected EOF")
                output.write_bytes(b"image")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(pipeline.time, "monotonic", return_value=0), patch.object(pipeline.time, "sleep") as sleep:
            output = Path(temp_dir) / "image-001.png"
            fake = FakeAI()
            pipeline._generate_image_with_retry(
                fake, "测试", [], output, Path(temp_dir), 1, 0, lambda _: None
            )

            self.assertEqual(fake.attempts, [1, 2])
            self.assertEqual(sleep.call_args_list[0].args, (10.0,))
            self.assertEqual(output.read_bytes(), b"image")

    def test_eof_retry_keeps_distinct_request_backups(self) -> None:
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return pipeline.httpx.Response(
                        500,
                        json={"error": {"message": "unexpected EOF"}},
                        request=pipeline.httpx.Request("POST", args[0]),
                    )
                return pipeline.httpx.Response(
                    200,
                    json={"data": [{"b64_json": "aW1hZ2U="}]},
                    request=pipeline.httpx.Request("POST", args[0]),
                )

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
        }, clear=False), patch.object(pipeline.httpx, "Client", return_value=FakeClient()), patch.object(pipeline.time, "sleep"):
            root = Path(temp_dir)
            source = root / "source.jpg"
            source.write_bytes(b"source")
            output = root / "raw-cards" / "image-001.png"
            output.parent.mkdir()
            fake = pipeline.OpenAICompatible()
            pipeline._generate_image_with_retry(
                fake, "测试", [source], output, root / "api-debug", 1, 0, lambda _: None
            )

            debug_dir = root / "api-debug"
            self.assertTrue((debug_dir / "image-001.postman.json").is_file())
            self.assertTrue((debug_dir / "image-001.response.body").is_file())
            self.assertTrue((debug_dir / "image-001.retry-1.postman.json").is_file())
            self.assertTrue((debug_dir / "image-001.retry-1.response.body").is_file())

    def test_failed_eof_retry_preserves_both_diagnostics(self) -> None:
        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def post(self, *args, **kwargs):
                return pipeline.httpx.Response(
                    500,
                    json={"error": {"message": "unexpected EOF"}},
                    request=pipeline.httpx.Request("POST", args[0]),
                )

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
        }, clear=False), patch.object(pipeline.httpx, "Client", return_value=FakeClient()), patch.object(pipeline.time, "sleep"):
            root = Path(temp_dir)
            source = root / "source.jpg"
            source.write_bytes(b"source")
            output = root / "raw-cards" / "image-002.png"
            output.parent.mkdir()

            with self.assertRaises(pipeline.PipelineError) as raised:
                pipeline._generate_image_with_retry(
                    pipeline.OpenAICompatible(), "测试", [source], output, root / "api-debug", 2, 0, lambda _: None
                )

            debug_dir = root / "api-debug"
            self.assertIn("image-002.postman.json", str(raised.exception))
            self.assertIn("image-002.retry-1.postman.json", str(raised.exception))
            self.assertEqual(len(list(debug_dir.glob("image-002*.postman.json"))), 2)
            self.assertEqual(len(list(debug_dir.glob("image-002*.response.body"))), 2)

    def test_non_eof_failure_is_not_retried(self) -> None:
        class FakeAI:
            def __init__(self):
                self.attempts = []

            def generate(self, prompt, references, output, request_dir=None, attempt=1):
                self.attempts.append(attempt)
                raise pipeline.PipelineError("图片生成 API 调用失败：HTTP 500；响应 body：服务暂时不可用")

        with tempfile.TemporaryDirectory() as temp_dir:
            fake = FakeAI()
            with self.assertRaises(pipeline.PipelineError):
                pipeline._generate_image_with_retry(
                    fake, "测试", [], Path(temp_dir) / "image.png", Path(temp_dir), 1, 0, lambda _: None
                )
            self.assertEqual(fake.attempts, [1])

    def test_image_start_times_are_staggered_by_configured_interval(self) -> None:
        class FakeAI:
            def generate(self, prompt, references, output, request_dir=None, attempt=1):
                output.write_bytes(b"image")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(pipeline.time, "monotonic", return_value=0), patch.object(pipeline.time, "sleep") as sleep:
            root = Path(temp_dir)
            fake = FakeAI()
            with patch.object(pipeline, "IMAGE_START_INTERVAL_SECONDS", 30.0):
                with ThreadPoolExecutor(max_workers=3) as pool:
                    list(pool.map(
                        lambda number: pipeline._generate_image_with_retry(
                            fake, "测试", [], root / f"image-{number}.png", root, number, 0, lambda _: None
                        ),
                        [1, 2, 3],
                    ))

            self.assertEqual(sorted(call.args[0] for call in sleep.call_args_list), [30.0, 60.0])

    def test_reference_paths_follow_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source_image = work_dir / "source.jpg"
            source_image.write_bytes(b"source")
            source_block = {"localPath": "source.jpg"}
            self.assertEqual(pipeline._reference_paths("copy", "off", source_block, "喜", work_dir), [source_image.resolve()])
            self.assertEqual(pipeline._reference_paths("rebuild", "off", source_block, "喜", work_dir), [])

    def test_image_edit_request_is_backed_up_for_postman(self) -> None:
        class FakeClient:
            def __init__(self, response):
                self.response = response
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def post(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return self.response

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_dir = root / "raw-cards"
            raw_dir.mkdir()
            source = root / "source image.jpg"
            brand = root / "喜1.png"
            source.write_bytes(b"source")
            brand.write_bytes(b"brand")
            output = raw_dir / "image-001.png"
            response = pipeline.httpx.Response(
                200,
                json={"data": [{"b64_json": "aW1hZ2U="}]},
                headers={"x-request-id": "req-test"},
                request=pipeline.httpx.Request("POST", "http://127.0.0.1:8317/v1/images/edits"),
            )
            fake = FakeClient(response)
            with patch.dict(os.environ, {
                "OPENAI_API_KEY": "secret-key-that-must-not-be-saved",
                "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
                "IMAGE_MODEL": "gpt-image-2",
            }, clear=False), patch.object(pipeline.httpx, "Client", return_value=fake):
                pipeline.OpenAICompatible().generate(
                    "画面提示\n包含中文文案：彼此麻烦。",
                    [source, brand],
                    output,
                )

            debug_dir = root / "api-debug"
            request_path = debug_dir / "image-001.request.json"
            postman_path = debug_dir / "image-001.postman.json"
            curl_path = debug_dir / "image-001.curl.txt"
            response_path = debug_dir / "image-001.response.body"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            collection = json.loads(postman_path.read_text(encoding="utf-8"))
            sent = fake.calls[0][1]

            self.assertEqual(sent["data"]["model"], "gpt-image-2")
            self.assertEqual(sent["data"]["prompt"], "画面提示\n包含中文文案：彼此麻烦。")
            self.assertEqual([item[0] for item in sent["files"]], ["image[]", "image[]"])
            self.assertEqual(request["response_status"], 200)
            self.assertEqual(request["files"][0]["path"], str(source.resolve()))
            self.assertEqual(request["files"][1]["path"], str(brand.resolve()))
            self.assertEqual(response_path.read_bytes(), response.content)
            self.assertIn("Bearer {{OPENAI_API_KEY}}", curl_path.read_text(encoding="utf-8"))
            self.assertEqual(collection["item"][0]["request"]["body"]["mode"], "formdata")
            self.assertEqual(
                [item["key"] for item in collection["item"][0]["request"]["body"]["formdata"]],
                ["model", "prompt", "size", "quality", "image[]", "image[]"],
            )
            self.assertNotIn("secret-key-that-must-not-be-saved", request_path.read_text(encoding="utf-8"))

    def test_image_api_error_saves_response_body_and_reports_paths(self) -> None:
        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def post(self, *args, **kwargs):
                return pipeline.httpx.Response(
                    500,
                    json={"error": {"message": "图片服务并发过高"}},
                    request=pipeline.httpx.Request("POST", args[0]),
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_dir = root / "raw-cards"
            raw_dir.mkdir()
            source = root / "source.jpg"
            source.write_bytes(b"source")
            output = raw_dir / "image-002.png"
            with patch.dict(os.environ, {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
            }, clear=False), patch.object(pipeline.httpx, "Client", return_value=FakeClient()):
                with self.assertRaises(pipeline.PipelineError) as raised:
                    pipeline.OpenAICompatible().generate("测试提示", [source], output)

            debug_dir = root / "api-debug"
            response_path = debug_dir / "image-002.response.body"
            request_path = debug_dir / "image-002.request.json"
            body = response_path.read_text(encoding="utf-8")
            self.assertIn("图片服务并发过高", body)
            self.assertIn("HTTP 500", str(raised.exception))
            self.assertIn(str(response_path), str(raised.exception))
            self.assertEqual(json.loads(request_path.read_text(encoding="utf-8"))["response_status"], 500)

    def test_image_failure_log_preserves_per_image_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            debug_dir = work_dir / "api-debug"
            debug_dir.mkdir()
            raw = work_dir / "raw-cards" / "image-001.png"
            raw.parent.mkdir()
            request = debug_dir / "image-001.postman.json"
            response = debug_dir / "image-001.response.body"
            request.write_text("{}", encoding="utf-8")
            response.write_text('{"error":"rate limited"}', encoding="utf-8")
            block = {"type": "sourceImageLink", "sourceIndex": 1}
            error = pipeline.PipelineError("HTTP 429；响应 body：rate limited")

            failure_log = pipeline._write_image_failure_log(
                work_dir,
                [(block, raw, work_dir / "cards" / "image-001.png", error)],
                [(block, error)],
            )

            self.assertIsNotNone(failure_log)
            details = json.loads(failure_log.read_text(encoding="utf-8"))
            self.assertEqual(details["count"], 1)
            self.assertEqual(details["failures"][0]["sourceIndex"], 1)
            self.assertIn("HTTP 429", details["failures"][0]["error"])
            self.assertEqual(details["failures"][0]["postmanRequest"], str(request))
            self.assertEqual(details["failures"][0]["responseBody"], str(response))

    def test_comic_planner_sends_text_only(self) -> None:
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"output_text": '{"title":"测试","blocks":[]}'}

        class FakeClient:
            calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def post(self, *args, **kwargs):
                self.calls.append(kwargs)
                return FakeResponse()

        source = {"sourceUrl": "https://mp.weixin.qq.com/s/x", "blocks": [{"type": "text", "sourceIndex": 0, "text": "正文"}, {"type": "image", "sourceIndex": 1, "localPath": "source.jpg"}]}
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            fake = FakeClient()
            with patch.object(pipeline.httpx, "Client", return_value=fake):
                pipeline.OpenAICompatible().plan(source, Path(temp_dir), "ori", "comic", 3, "ai", lambda _: None, "off")
            content = fake.calls[-1]["json"]["input"][0]["content"]
            self.assertEqual([item for item in content if item.get("type") == "input_image"], [])
            self.assertIn("恰好 3 个连续叙事节点", content[0]["text"])

    def test_direct_fetch_is_used_by_pipeline(self) -> None:
        content = (ROOT / "website_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("正在直接获取微信文章页面", content)
        self.assertIn('SCRIPTS / "fetch_article.py"', content)
        self.assertNotIn("N8N" + "_WEBHOOK_URL", content)


if __name__ == "__main__":
    unittest.main()
