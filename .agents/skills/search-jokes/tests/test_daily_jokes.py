import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import daily_jokes


REGISTRY = SKILL_ROOT / "data" / "joke_registry.json"


class DailyJokesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = daily_jokes.load_registry(REGISTRY)["records"]

    def test_demo_reference_and_initial_delivery_are_valid(self):
        registry = {"schema_version": 1, "records": self.records}
        self.assertEqual(daily_jokes.validate_registry(registry), [])
        initial = [record for record in self.records if record.get("batch_id") == "2026-09-21-initial"]
        self.assertEqual(len(initial), 10)
        self.assertTrue(all(record["source"]["url"].startswith("https://") for record in initial))
        self.assertTrue(all(60 <= daily_jokes.character_count(record["text"]) <= 185 for record in initial))

    def test_v2_delivery_meets_the_new_gate(self):
        v2 = [record for record in self.records if record.get("batch_id") == "2026-09-21-v2"]
        self.assertEqual(len(v2), 10)
        self.assertTrue(all(record["selection_policy"] == daily_jokes.PUSH_POLICY_V2 for record in v2))
        self.assertTrue(all(daily_jokes.meets_push_threshold(record) for record in v2))

    def test_exact_duplicate_is_rejected(self):
        duplicate = copy.deepcopy(self.records[0])
        duplicate["id"] = "duplicate-exact"
        self.assertIn("原文重复", daily_jokes.duplicate_reason(duplicate, self.records) or "")

    def test_same_punchline_with_new_wording_is_rejected(self):
        duplicate = copy.deepcopy(self.records[2])
        duplicate["id"] = "duplicate-semantic"
        duplicate["text"] = "饭桌上大家又催我结婚。\n我说再提就掀桌。\n第二年，他们换成了石头桌子。"
        self.assertIn("核心包袱重复", daily_jokes.duplicate_reason(duplicate, self.records) or "")

    def test_highly_similar_wording_is_rejected(self):
        duplicate = copy.deepcopy(self.records[4])
        duplicate["id"] = "duplicate-fuzzy"
        duplicate["text"] = duplicate["text"].replace("这么多年", "这些年")
        duplicate["semantic_signature"] = {
            "relationship": "different",
            "setting": "different",
            "premise": "different",
            "punchline": "different",
        }
        self.assertIn("措辞高度相似", daily_jokes.duplicate_reason(duplicate, self.records) or "")

    def test_safety_boundary_rejects_forbidden_content(self):
        unsafe = copy.deepcopy(self.records[0])
        unsafe["id"] = "unsafe"
        unsafe["text"] = "这是一个政治笑话。"
        self.assertTrue(any("内容边界词" in error for error in daily_jokes.validate_shape(unsafe)))

    def test_add_materializes_metadata_before_persisting(self):
        candidate = {
            "id": "new-unique-record",
            "text": "朋友问我为什么总带伞。\n我说：\n“因为天气预报说今天有百分之五十的概率下雨，\n我决定站在另外百分之五十那边。”",
            "source": {"title": "测试来源", "url": "https://example.com/joke"},
            "accessed_at": "2026-09-21",
            "tags": ["日常", "朋友"],
            "twist_type": "逻辑反转",
            "state": "shown",
            "batch_id": "test-batch",
            "dialogue_turn_count": 8,
            "semantic_signature": {
                "relationship": "friends",
                "setting": "outdoors",
                "premise": "rain-probability",
                "punchline": "choose-other-half",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            registry_path = temporary_path / "registry.json"
            input_path = temporary_path / "candidate.json"
            registry_path.write_text(json.dumps({"schema_version": 1, "records": self.records}, ensure_ascii=False), encoding="utf-8")
            input_path.write_text(json.dumps([candidate], ensure_ascii=False), encoding="utf-8")
            result = daily_jokes.command_add(SimpleNamespace(registry=registry_path, input=input_path, state=None))
            stored = daily_jokes.load_registry(registry_path)["records"][-1]
        self.assertEqual(result, 0)
        self.assertEqual(stored["character_count"], daily_jokes.character_count(candidate["text"]))
        self.assertTrue(stored["fingerprints"]["normalized_sha256"])

    def test_v2_candidate_needs_length_or_eight_dialogue_turns(self):
        candidate = copy.deepcopy(self.records[0])
        candidate.update({
            "id": "too-short-v2",
            "text": "朋友说：今天真热。\n我说：是啊。",
            "selection_policy": daily_jokes.PUSH_POLICY_V2,
            "dialogue_turn_count": 2,
        })
        candidate = daily_jokes.enrich(candidate)
        self.assertTrue(any("不满足推送门槛" in error for error in daily_jokes.validate_shape(candidate)))

    @staticmethod
    def new_candidate(item_id, text, signature, batch_id="test-collection"):
        return {
            "id": item_id,
            "text": text,
            "source": {"title": "测试来源", "url": f"https://example.com/{item_id}"},
            "accessed_at": "2026-09-23",
            "tags": ["日常", "朋友"],
            "twist_type": "逻辑反转",
            "state": "shown",
            "batch_id": batch_id,
            "dialogue_turn_count": 8,
            "semantic_signature": signature,
        }

    def test_screen_is_non_mutating_and_reports_all_rejection_layers(self):
        valid = self.new_candidate(
            "screen-valid",
            "朋友问我为什么每次开会都带一支铅笔。\n我说：万一会议没有结论，至少还能把它削尖一点。",
            {"relationship": "friends", "setting": "office", "premise": "meeting-preparation", "punchline": "sharpen-pencil"},
        )
        semantic = self.new_candidate(
            "screen-semantic",
            "饭桌上大家又问我什么时候结婚。\n我说再问就掀桌子。\n第二年，他们真的换了一张大理石桌继续问。",
            copy.deepcopy(self.records[2]["semantic_signature"]),
        )
        short = self.new_candidate(
            "screen-short",
            "朋友说：今天真热。\n我说：是啊。",
            {"relationship": "friends", "setting": "outdoors", "premise": "hot-weather", "punchline": "agreement"},
        )
        short["dialogue_turn_count"] = 2
        duplicate_in_batch = copy.deepcopy(valid)
        duplicate_in_batch["id"] = "screen-batch-duplicate"

        original_count = len(self.records)
        accepted, rejected = daily_jokes.screen_candidates(
            [valid, semantic, short, duplicate_in_batch], self.records
        )
        self.assertEqual([item["id"] for item in accepted], ["screen-valid"])
        reasons = "\n".join(reason for _, reason in rejected)
        self.assertIn("核心包袱重复", reasons)
        self.assertIn("不满足推送门槛", reasons)
        self.assertIn("原文重复", reasons)
        self.assertEqual(len(self.records), original_count)

    def test_screen_then_add_only_persists_selected_items_across_batches(self):
        first = self.new_candidate(
            "batch-first",
            "同事问我为什么总是最后一个离开办公室。\n我说：因为我负责确认所有人都已经先走了。",
            {"relationship": "coworkers", "setting": "office", "premise": "last-to-leave", "punchline": "confirm-departure"},
        )
        second = self.new_candidate(
            "batch-second",
            "朋友说我做决定太慢。\n我说这件事我已经考虑过了。\n朋友问结果呢。\n我说：我决定再考虑一下。",
            {"relationship": "friends", "setting": "cafe", "premise": "slow-decisions", "punchline": "consider-again"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            registry_path = temporary_path / "registry.json"
            first_input = temporary_path / "first.json"
            first_accepted = temporary_path / "first-accepted.json"
            second_input = temporary_path / "second.json"
            second_accepted = temporary_path / "second-accepted.json"
            registry_path.write_text(json.dumps({"schema_version": 1, "records": self.records}, ensure_ascii=False), encoding="utf-8")
            first_input.write_text(json.dumps([first], ensure_ascii=False), encoding="utf-8")
            daily_jokes.command_screen(SimpleNamespace(registry=registry_path, input=first_input, limit=1, state="shown", accepted_output=first_accepted))
            daily_jokes.command_add(SimpleNamespace(registry=registry_path, input=first_accepted, state="shown"))

            second_input.write_text(json.dumps([first, second], ensure_ascii=False), encoding="utf-8")
            daily_jokes.command_screen(SimpleNamespace(registry=registry_path, input=second_input, limit=1, state="shown", accepted_output=second_accepted))
            daily_jokes.command_add(SimpleNamespace(registry=registry_path, input=second_accepted, state="shown"))

            stored = daily_jokes.load_registry(registry_path)["records"]
        added = [item for item in stored if item.get("batch_id") == "test-collection"]
        self.assertEqual([item["id"] for item in added], ["batch-first", "batch-second"])
        self.assertTrue(all(item["state"] == "shown" for item in added))


if __name__ == "__main__":
    unittest.main()
