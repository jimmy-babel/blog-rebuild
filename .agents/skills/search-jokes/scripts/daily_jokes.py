#!/usr/bin/env python3
"""Local archive, duplicate gate, and renderer for the 每日一笑 collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_ROOT / "data" / "joke_registry.json"
WHITESPACE = re.compile(r"\s+")
FORBIDDEN = (
    "色情", "裸聊", "强奸", "自杀", "种族", "歧视", "纳粹", "政治笑话",
)
PUSH_POLICY_V2 = "v2-min-100-chars-or-8-dialogue-turns"
REQUIRED = {
    "id", "text", "source", "accessed_at", "tags", "twist_type", "state",
    "semantic_signature", "batch_id", "character_count", "line_count", "fingerprints",
}


def normalized(text: str) -> str:
    """Normalise comparison-only text without changing the stored original."""
    text = text.casefold().translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    return WHITESPACE.sub("", text)


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()


def character_count(text: str) -> int:
    return len(WHITESPACE.sub("", text))


def line_count(text: str) -> int:
    return sum(bool(line.strip()) for line in text.splitlines())


def meets_push_threshold(record: dict[str, Any]) -> bool:
    """New delivery gate: original text ≥100 chars, or ≥8 dialogue turns."""
    return character_count(record["text"]) >= 100 or record.get("dialogue_turn_count", 0) >= 8


def enrich(record: dict[str, Any]) -> dict[str, Any]:
    """Add deterministic, machine-derived metadata to a record copy."""
    item = dict(record)
    item["character_count"] = character_count(item["text"])
    item["line_count"] = line_count(item["text"])
    item["fingerprints"] = {
        "normalized_sha256": fingerprint(item["text"]),
        "semantic_key": semantic_key(item["semantic_signature"]),
    }
    return item


def semantic_key(signature: dict[str, str]) -> str:
    fields = ("relationship", "setting", "premise", "punchline")
    return "|".join(signature.get(field, "").strip().casefold() for field in fields)


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalized(left), normalized(right)).ratio()


def semantic_match(left: dict[str, str], right: dict[str, str]) -> bool:
    """Reject the same comic premise even when wording was changed.

    Signatures are deliberately explicit: a candidate matches when relationship and
    setting agree and either its premise or its final punchline also agrees.
    """
    same_context = (
        left.get("relationship") == right.get("relationship")
        and left.get("setting") == right.get("setting")
    )
    same_core = (
        left.get("premise") == right.get("premise")
        or left.get("punchline") == right.get("punchline")
    )
    return same_context and same_core


def safety_issues(text: str) -> list[str]:
    compact = normalized(text)
    return [term for term in FORBIDDEN if term in compact]


def load_registry(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        registry = json.load(handle)
    if registry.get("schema_version") != 1 or not isinstance(registry.get("records"), list):
        raise ValueError("档案格式不正确：需要 schema_version=1 和 records 数组。")
    return registry


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(registry, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def validate_shape(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED - record.keys())
    if missing:
        errors.append(f"缺少字段：{', '.join(missing)}")
        return errors
    if not isinstance(record["source"], dict) or not record["source"].get("title") or not record["source"].get("url"):
        errors.append("source 必须包含 title 和 url")
    if not isinstance(record["tags"], list) or not record["tags"]:
        errors.append("tags 必须是非空数组")
    signature = record["semantic_signature"]
    if not isinstance(signature, dict) or any(not signature.get(name) for name in ("relationship", "setting", "premise", "punchline")):
        errors.append("semantic_signature 必须包含 relationship、setting、premise、punchline")
    if record["state"] not in {"reference", "shown", "adopted", "candidate"}:
        errors.append("state 只能是 reference、shown、adopted 或 candidate")
    if not isinstance(record["text"], str) or not record["text"].strip():
        errors.append("text 不能为空")
    if isinstance(record.get("text"), str):
        expected = enrich(record)
        if record.get("character_count") != expected["character_count"]:
            errors.append("character_count 与原文不一致")
        if record.get("line_count") != expected["line_count"]:
            errors.append("line_count 与原文不一致")
        if record.get("fingerprints") != expected["fingerprints"]:
            errors.append("fingerprints 与原文或语义签名不一致")
    if record.get("selection_policy") == PUSH_POLICY_V2:
        turns = record.get("dialogue_turn_count")
        if not isinstance(turns, int) or turns < 0:
            errors.append("dialogue_turn_count 必须是非负整数")
        elif not meets_push_threshold(record):
            errors.append("不满足推送门槛：原文字数需≥100，或对话轮数需≥8")
    issues = safety_issues(record.get("text", ""))
    if issues:
        errors.append(f"命中内容边界词：{', '.join(issues)}")
    return errors


def duplicate_reason(candidate: dict[str, Any], records: list[dict[str, Any]]) -> str | None:
    candidate_hash = fingerprint(candidate["text"])
    for existing in records:
        if candidate_hash == fingerprint(existing["text"]):
            return f"原文重复：{existing['id']}"
        score = similarity(candidate["text"], existing["text"])
        if score >= 0.88:
            return f"措辞高度相似（{score:.0%}）：{existing['id']}"
        if semantic_match(candidate["semantic_signature"], existing["semantic_signature"]):
            return f"核心包袱重复：{existing['id']}"
    return None


def validate_registry(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    accepted: list[dict[str, Any]] = []
    for record in registry["records"]:
        prefix = f"{record.get('id', '<无 id>')}："
        if record.get("id") in seen_ids:
            errors.append(prefix + "重复 id")
        seen_ids.add(record.get("id"))
        errors.extend(prefix + error for error in validate_shape(record))
        if not validate_shape(record):
            duplicate = duplicate_reason(record, accepted)
            if duplicate:
                errors.append(prefix + duplicate)
            accepted.append(record)
    return errors


def command_validate(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    errors = validate_registry(registry)
    if errors:
        print("档案校验失败：", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    records = [enrich(item) for item in registry["records"]]
    states = Counter(item["state"] for item in records)
    lengths = [item["character_count"] for item in records]
    qualified = sum(
        record.get("selection_policy") == PUSH_POLICY_V2 and meets_push_threshold(record)
        for record in records
    )
    print(f"校验通过：{len(records)} 条；状态 {dict(states)}；长度 {min(lengths)}–{max(lengths)} 字；新规则合格 {qualified} 条。")
    return 0


def command_add(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    with args.input.open("r", encoding="utf-8") as handle:
        incoming = json.load(handle)
    candidates = incoming["records"] if isinstance(incoming, dict) else incoming
    if not isinstance(candidates, list):
        raise ValueError("输入必须是 records 数组或包含 records 的对象。")
    added, rejected = 0, []
    for candidate in candidates:
        if args.state:
            candidate["state"] = args.state
        candidate.setdefault("selection_policy", PUSH_POLICY_V2)
        candidate = enrich(candidate)
        errors = validate_shape(candidate)
        reason = duplicate_reason(candidate, registry["records"]) if not errors else None
        if errors or reason:
            rejected.append((candidate.get("id", "<无 id>"), "; ".join(errors) if errors else reason))
            continue
        registry["records"].append(candidate)
        added += 1
    if added:
        save_registry(args.registry, registry)
    print(f"已入库 {added} 条；拦截 {len(rejected)} 条。")
    for item_id, reason in rejected:
        print(f"- {item_id}：{reason}")
    return 1 if rejected else 0


def command_refresh(args: argparse.Namespace) -> int:
    """Persist calculated counts and fingerprints for records imported by hand."""
    registry = load_registry(args.registry)
    registry["records"] = [enrich(record) for record in registry["records"]]
    save_registry(args.registry, registry)
    print(f"已刷新 {len(registry['records'])} 条记录的长度和指纹。")
    return 0


def source_markdown(record: dict[str, Any]) -> str:
    source = record["source"]
    return f"来源：[{source['title']}]({source['url']})"


def command_render(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    records = [item for item in registry["records"] if item["state"] in {"shown", "adopted"}]
    batch = args.batch
    if not batch:
        batches = [item["batch_id"] for item in records if item.get("batch_id")]
        batch = batches[-1] if batches else ""
    records = [item for item in records if item.get("batch_id") == batch]
    if not records:
        print("没有可输出的批次。", file=sys.stderr)
        return 1
    print(f"# 每日一笑｜{batch}\n")
    for index, record in enumerate(records[: args.limit], start=1):
        meta = enrich(record)
        print(f"## {index}. {record['twist_type']}｜{'、'.join(record['tags'])}")
        print(record["text"].strip())
        print(f"\n{source_markdown(record)} · {meta['character_count']} 字\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日一笑档案、去重和展示工具")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY, help="档案 JSON 路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="校验字段、安全边界和三层去重")
    add = sub.add_parser("add", help="将联网筛出的候选 JSON 入库")
    add.add_argument("input", type=Path, help="候选 JSON 文件")
    add.add_argument("--state", choices=["candidate", "shown", "adopted"], help="覆盖候选状态")
    render = sub.add_parser("render", help="按批次输出已展示笑话")
    render.add_argument("--batch", help="批次 ID；省略时取最近批次")
    render.add_argument("--limit", type=int, default=10, help="最多输出条数")
    sub.add_parser("refresh", help="将长度、行数和指纹写回手工导入的档案")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "validate":
        return command_validate(args)
    if args.command == "add":
        return command_add(args)
    if args.command == "refresh":
        return command_refresh(args)
    return command_render(args)


if __name__ == "__main__":
    raise SystemExit(main())
