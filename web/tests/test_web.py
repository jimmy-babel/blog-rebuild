from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module  # noqa: E402
import website_pipeline as pipeline  # noqa: E402


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)
        self.client.__enter__()
        app_module.DB.init()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_health_and_index(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("文章重构工作台", response.text)

    def test_url_validation(self) -> None:
        response = self.client.post("/api/tasks", json={"url": "https://example.com/a"})
        self.assertEqual(response.status_code, 422)

    def test_brand_defaults_and_legacy_alias(self) -> None:
        with patch.object(app_module, "schedule"):
            default_response = self.client.post(
                "/api/tasks", json={"url": "https://mp.weixin.qq.com/s/brand-default"}
            )
            legacy_response = self.client.post(
                "/api/tasks", json={"url": "https://mp.weixin.qq.com/s/brand-legacy", "brand_reference": "off"}
            )
        self.assertEqual(default_response.status_code, 202)
        self.assertEqual(default_response.json()["brand"], "on")
        self.assertEqual(default_response.json()["brand_reference"], "on")
        self.assertEqual(legacy_response.status_code, 202)
        self.assertEqual(legacy_response.json()["brand"], "off")
        self.assertEqual(legacy_response.json()["brand_reference"], "off")
        app_module.DB.delete(default_response.json()["id"])
        app_module.DB.delete(legacy_response.json()["id"])

    def test_brand_alias_conflict_is_rejected(self) -> None:
        response = self.client.post(
            "/api/tasks",
            json={
                "url": "https://mp.weixin.qq.com/s/brand-conflict",
                "brand": "on",
                "brand_reference": "off",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_create_list_search_and_delete_without_running_pipeline(self) -> None:
        with patch.object(app_module, "schedule"):
            response = self.client.post(
                "/api/tasks",
                json={"url": "https://mp.weixin.qq.com/s/test-web-mvp", "image_mode": "rebuild", "caption_mode": "ai"},
            )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["id"]
        listed = self.client.get("/api/tasks", params={"q": task_id})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(self.client.get(f"/api/tasks/{task_id}").json()["status"], "queued")
        deleted = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get(f"/api/tasks/{task_id}").status_code, 404)

    def test_processing_task_cannot_be_deleted(self) -> None:
        task_id = "processing-test"
        app_module.DB.create(task_id, "https://mp.weixin.qq.com/s/processing", "copy", "ai")
        app_module.DB.update(task_id, status="processing")
        response = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(response.status_code, 409)
        app_module.DB.delete(task_id)

    def test_result_route_serves_html_and_relative_image(self) -> None:
        task_id = "result-route-test"
        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            (result_dir / "images").mkdir()
            (result_dir / "article.html").write_text(
                '<!doctype html><img src="images/image-001.png">', encoding="utf-8"
            )
            (result_dir / "images" / "image-001.png").write_bytes(b"png")
            app_module.DB.create(task_id, "https://mp.weixin.qq.com/s/result", "copy", "ai")
            app_module.DB.update(
                task_id, status="completed", result_dir=str(result_dir), html_file=str(result_dir / "article.html")
            )
            html = self.client.get(f"/results/{task_id}/article.html")
            image = self.client.get(f"/results/{task_id}/images/image-001.png")
            self.assertEqual(html.status_code, 200)
            self.assertEqual(image.status_code, 200)
            app_module.DB.delete(task_id)


class WebsitePipelineTests(unittest.TestCase):
    def test_planner_only_attaches_brand_reference_when_enabled(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"output_text": '{"title":"测试标题","blocks":[]}'}

        class FakeClient:
            calls: list[dict] = []

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def post(self, *args: object, **kwargs: dict) -> FakeResponse:
                self.calls.append(kwargs)
                return FakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            source = {"sourceUrl": "https://mp.weixin.qq.com/s/test", "blocks": []}
            fake = FakeClient()
            with patch.object(pipeline.httpx, "Client", return_value=fake):
                pipeline.OpenAICompatible().plan(source, Path(temp_dir), "rebuild", "ai", lambda _: None, "off")
            off_content = fake.calls[-1]["json"]["input"][0]["content"]
            self.assertEqual([item for item in off_content if item.get("type") == "input_image"], [])

            with patch.object(pipeline.httpx, "Client", return_value=fake):
                pipeline.OpenAICompatible().plan(source, Path(temp_dir), "rebuild", "ai", lambda _: None, "on")
            on_content = fake.calls[-1]["json"]["input"][0]["content"]
            self.assertEqual(len([item for item in on_content if item.get("type") == "input_image"]), 1)

    def test_reference_paths_follow_skill_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            source_image = work_dir / "source.jpg"
            source_image.write_bytes(b"source")
            source_block = {"localPath": "source.jpg"}
            brand_image = pipeline._emotion_reference("喜")

            self.assertEqual(
                pipeline._reference_paths("copy", "on", source_block, "喜", work_dir),
                [source_image.resolve(), brand_image],
            )
            self.assertEqual(
                pipeline._reference_paths("rebuild", "on", source_block, "喜", work_dir),
                [brand_image],
            )
            self.assertEqual(
                pipeline._reference_paths("copy", "off", source_block, "喜", work_dir),
                [source_image.resolve()],
            )
            self.assertEqual(pipeline._reference_paths("rebuild", "off", source_block, "喜", work_dir), [])

    def test_brand_off_prompt_does_not_request_brand_reference(self) -> None:
        self.assertNotIn("品牌人物", pipeline._reference_instruction("copy", "off"))
        self.assertIn("品牌人物", pipeline._reference_instruction("copy", "on"))

    def test_generations_rejects_reference_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"image")
            with patch.dict(os.environ, {"IMAGE_API_MODE": "generations"}, clear=False):
                with self.assertRaisesRegex(pipeline.PipelineError, "generations.*不支持参考图"):
                    pipeline.OpenAICompatible().generate("test", [reference], Path(temp_dir) / "out.png")


if __name__ == "__main__":
    unittest.main()
