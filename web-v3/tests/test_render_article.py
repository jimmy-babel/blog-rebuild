from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_article  # noqa: E402


class RenderArticleTests(unittest.TestCase):
    def make_manifest(
        self, directory: Path, title: str = "测试文章", with_image: bool = False
    ) -> Path:
        blocks = [{"type": "text", "text": "正文。"}]
        if with_image:
            (directory / "asset.png").write_bytes(b"png")
            blocks.append({"type": "image", "path": "asset.png", "alt": "配图"})
        manifest = {
            "title": title,
            "sourceUrl": "https://mp.weixin.qq.com/s/test",
            "articleMode": "ori",
            "imageMode": "copy",
            "blocks": blocks,
        }
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    def test_existing_package_gets_timestamped_name_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result_dir = root / "result"
            result_dir.mkdir()
            existing = result_dir / "0911-测试文章"
            existing.mkdir()
            sentinel = existing / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            manifest = self.make_manifest(root)

            rendered = render_article.render(
                manifest,
                result_dir,
                now=datetime(2026, 9, 11, 15, 40, 0),
            )

            self.assertNotEqual(Path(rendered["packageDir"]), existing)
            self.assertTrue(sentinel.is_file())
            self.assertTrue(Path(rendered["html"]).is_file())
            self.assertEqual(Path(rendered["html"]).name, Path(rendered["packageDir"]).name + ".html")

    def test_render_allows_soft_limit_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = {
                "title": "长度测试",
                "sourceUrl": "https://mp.weixin.qq.com/s/test",
                "articleMode": "trans",
                "imageMode": "copy",
                "sourceCharacterCount": 205,
                "blocks": [{"type": "text", "text": "改" * 282}],
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            rendered = render_article.render(manifest_path, root / "result")

            self.assertTrue(Path(rendered["html"]).is_file())
            self.assertEqual(rendered["sourceCharacterCount"], 205)
            self.assertEqual(rendered["rewriteCharacterLimit"], 144)

    def test_render_allows_large_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = {
                "title": "长度测试",
                "sourceUrl": "https://mp.weixin.qq.com/s/test",
                "articleMode": "trans",
                "imageMode": "copy",
                "sourceCharacterCount": 205,
                "blocks": [{"type": "text", "text": "超" * 801}],
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            rendered = render_article.render(manifest_path, root / "result")

            self.assertTrue(Path(rendered["html"]).is_file())

    def test_commit_retries_when_target_appears_before_rename(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result_dir = root / "result"
            manifest = self.make_manifest(root)
            target = result_dir / "0911-测试文章"
            original_rename = Path.rename
            state = {"raced": False}

            def race_once(source: Path, destination: Path) -> Path:
                if not state["raced"]:
                    state["raced"] = True
                    destination.mkdir(parents=True)
                    raise PermissionError(5, "拒绝访问", str(source), str(destination))
                return original_rename(source, destination)

            with patch.object(Path, "rename", race_once):
                rendered = render_article.render(
                    manifest,
                    result_dir,
                    now=datetime(2026, 9, 11, 15, 40, 0),
                )

            package_dir = Path(rendered["packageDir"])
            self.assertTrue(state["raced"])
            self.assertNotEqual(package_dir, target)
            self.assertTrue(package_dir.is_dir())
            self.assertTrue(Path(rendered["html"]).is_file())
            self.assertEqual(Path(rendered["html"]).name, package_dir.name + ".html")

    def test_commit_retries_transient_winerror5_when_target_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result_dir = root / "result"
            result_dir.mkdir()
            staging_dir = result_dir / ".staging"
            staging_dir.mkdir()
            html = staging_dir / "0911-测试文章.html"
            html.write_text("ok", encoding="utf-8")
            target = result_dir / "0911-测试文章"
            original_rename = Path.rename
            state = {"failures": 0}

            def fail_twice(source: Path, destination: Path) -> Path:
                if state["failures"] < 2:
                    state["failures"] += 1
                    raise PermissionError(5, "拒绝访问", str(source), str(destination))
                return original_rename(source, destination)

            with patch.object(render_article.Path, "rename", fail_twice), patch.object(render_article.time, "sleep") as sleep:
                package_dir, html_name = render_article.commit_staging_dir(
                    staging_dir, target, html.name, result_dir, "测试文章", datetime(2026, 9, 11, 15, 40, 0)
                )

            self.assertEqual(state["failures"], 2)
            self.assertEqual(package_dir, target)
            self.assertEqual(html_name, "0911-测试文章.html")
            self.assertTrue((target / html_name).is_file())
            self.assertEqual(sleep.call_count, 2)

    def test_commit_switches_name_if_target_appears_during_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result_dir = root / "result"
            result_dir.mkdir()
            staging_dir = result_dir / ".staging"
            staging_dir.mkdir()
            html = staging_dir / "0911-测试文章.html"
            html.write_text("ok", encoding="utf-8")
            target = result_dir / "0911-测试文章"
            original_rename = Path.rename
            state = {"calls": 0}

            def target_appears_on_retry(source: Path, destination: Path) -> Path:
                state["calls"] += 1
                if state["calls"] == 1:
                    raise PermissionError(5, "拒绝访问", str(source), str(destination))
                if state["calls"] == 2:
                    destination.mkdir()
                    raise PermissionError(5, "拒绝访问", str(source), str(destination))
                return original_rename(source, destination)

            with patch.object(render_article.Path, "rename", target_appears_on_retry), patch.object(render_article.time, "sleep"):
                package_dir, html_name = render_article.commit_staging_dir(
                    staging_dir, target, html.name, result_dir, "测试文章", datetime(2026, 9, 11, 15, 40, 0)
                )

            self.assertNotEqual(package_dir, target)
            self.assertTrue((package_dir / html_name).is_file())
            self.assertEqual(html_name, package_dir.name + ".html")

    def test_commit_stops_after_bounded_winerror5_retries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result_dir = root / "result"
            result_dir.mkdir()
            staging_dir = result_dir / ".staging"
            staging_dir.mkdir()
            html = staging_dir / "0911-测试文章.html"
            html.write_text("ok", encoding="utf-8")
            target = result_dir / "0911-测试文章"

            def always_denied(source: Path, destination: Path) -> Path:
                raise PermissionError(5, "拒绝访问", str(source), str(destination))

            with patch.object(render_article.Path, "rename", always_denied), patch.object(render_article.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    render_article.commit_staging_dir(
                        staging_dir, target, html.name, result_dir, "测试文章", datetime(2026, 9, 11, 15, 40, 0)
                    )

            self.assertEqual(sleep.call_count, len(render_article.RENAME_RETRY_DELAYS))
            self.assertTrue(staging_dir.is_dir())

    def test_concurrent_same_title_renders_both_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result_dir = root / "result"
            manifest = self.make_manifest(root, title="并发文章", with_image=True)
            forced_name = result_dir / "0911-并发文章"

            def render_one() -> dict:
                return render_article.render(
                    manifest,
                    result_dir,
                    now=datetime(2026, 9, 11, 15, 40, 0),
                )

            with patch.object(render_article, "unique_package_dir", return_value=forced_name):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    rendered = list(pool.map(lambda _: render_one(), range(2)))

            package_dirs = {Path(item["packageDir"]) for item in rendered}
            self.assertEqual(len(package_dirs), 2)
            for item in rendered:
                package_dir = Path(item["packageDir"])
                self.assertTrue(package_dir.is_dir())
                self.assertTrue(Path(item["html"]).is_file())
                self.assertTrue((package_dir / "images" / "image-001.png").is_file())
                self.assertEqual(Path(item["html"]).name, package_dir.name + ".html")


if __name__ == "__main__":
    unittest.main()
