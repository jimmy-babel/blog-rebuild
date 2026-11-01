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
DEFAULT_EMOJI_FILE = SKILL_ROOT / "data" / "emoji.txt"
WHITESPACE = re.compile(r"\s+")
EMOJI_CODEPOINT = re.compile(r"[\U0001F300-\U0001FAFF]|[\u2600-\u27BF]|[©®™]")
FORBIDDEN = (
    "色情", "裸聊", "强奸", "自杀", "种族", "歧视", "纳粹", "政治笑话",
)
PUSH_POLICY_V2 = "v2-min-100-chars-or-8-dialogue-turns"
DEFAULT_MIN_COUNT = 90
DEFAULT_MIN_DIALOGUE = 6
DEFAULT_COUNT_LIMIT = 150
PUSH_POLICY_V3_PREFIX = "v3-min-"
REQUIRED = {
    "id", "text", "source", "accessed_at", "tags", "twist_type", "state",
    "semantic_signature", "batch_id", "character_count", "line_count", "fingerprints",
}


def load_emojis(path: Path = DEFAULT_EMOJI_FILE) -> list[str]:
    """Load the display-only emoji palette from one emoji-per-line text."""
    if not path.exists():
        raise FileNotFoundError(f"Emoji 资源不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
        emojis = [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]
    if not emojis:
        raise ValueError(f"Emoji 资源为空：{path}")
    return list(dict.fromkeys(emojis))


def has_emoji(text: str) -> bool:
    """Return whether a line already contains an emoji-like code point."""
    return bool(EMOJI_CODEPOINT.search(text))


def choose_emoji(line: str, line_index: int, emojis: list[str], previous: str | None = None) -> str:
    """Choose a stable, context-aware emoji without using Python's random hash."""
    semantic_choices = (
        (("?", "？"), ("🤔", "🧐")),
        (("差点", "晕倒", "没想到", "原来", "居然", "竟然", "发现", "不是"), ("🤯", "🙃")),
        (("哭", "伤心", "难过", "眼泪"), ("😭", "☹️")),
        (("生气", "小气", "吼", "怒", "杀气"), ("😤", "😠")),
        (("笑", "哈哈", "满意", "不错", "鼓掌", "成功"), ("🤣", "😄", "👍", "👏")),
    )
    for cues, choices in semantic_choices:
        if any(cue in line for cue in cues):
            for candidate in choices:
                if candidate in emojis and candidate != previous:
                    return candidate

    digest = hashlib.sha256(f"{line_index}:{line}".encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % len(emojis)
    for offset in range(len(emojis)):
        candidate = emojis[(start + offset) % len(emojis)]
        if candidate != previous:
            return candidate
    return emojis[start]


def decorate_text(text: str, emoji_path: Path = DEFAULT_EMOJI_FILE) -> str:
    """Add display-only emoji while preserving the stored joke text unchanged.

    The first and last non-empty lines always have an emoji. Between them, every
    third non-empty line is decorated, producing a stable 2–3 line cadence.
    Existing emoji are kept and never duplicated.
    """
    emojis = load_emojis(emoji_path)
    lines = text.strip().splitlines()
    if not lines:
        return text

    non_empty_indices = [index for index, line in enumerate(lines) if line.strip()]
    first_non_empty = non_empty_indices[0]
    last_non_empty = non_empty_indices[-1]
    decorated: list[str] = []
    previous_emoji: str | None = None

    for index, line in enumerate(lines):
        should_decorate = (
            index == first_non_empty
            or index == last_non_empty
            or (index > first_non_empty and (index - first_non_empty) % 3 == 0)
        )
        current = line
        if line.strip() and should_decorate and not has_emoji(line):
            chosen = choose_emoji(line, index, emojis, previous_emoji)
            current = f"{line.rstrip()}{chosen}"
            previous_emoji = chosen
        elif line.strip() and has_emoji(line):
            previous_emoji = next((emoji for emoji in emojis if emoji in line), previous_emoji)
        decorated.append(current)
    return "\n".join(decorated)


def ensure_utf8_stdout() -> None:
    """Allow Windows console and redirected output to carry the emoji palette."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None and (getattr(sys.stdout, "encoding", "") or "").lower() not in {"utf-8", "utf8"}:
        reconfigure(encoding="utf-8")


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


def make_selection_policy(min_count: int, min_dialogue: int, count_limit: int) -> str:
    return f"{PUSH_POLICY_V3_PREFIX}{min_count}-chars-or-{min_dialogue}-dialogue-turns-max-{count_limit}-chars"


def selection_limits(policy: str | None) -> tuple[int, int, int | None] | None:
    """Return (minimum chars, minimum dialogue turns, maximum chars) for a policy."""
    if policy == PUSH_POLICY_V2:
        return 100, 8, None
    if not isinstance(policy, str) or not policy.startswith(PUSH_POLICY_V3_PREFIX):
        return None
    match = re.fullmatch(r"v3-min-(\d+)-chars-or-(\d+)-dialogue-turns-max-(\d+)-chars", policy)
    if not match:
        return None
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def validate_policy_limits(min_count: int, min_dialogue: int, count_limit: int) -> None:
    if min_count < 0 or min_dialogue < 0:
        raise ValueError("count 和 topic 必须是非负整数。")
    if count_limit < 1:
        raise ValueError("countLimit 必须是正整数。")
    if count_limit < min_count:
        raise ValueError("countLimit 必须大于或等于 count。")


def meets_push_threshold(record: dict[str, Any]) -> bool:
    """Return whether a record satisfies its stored delivery policy."""
    limits = selection_limits(record.get("selection_policy"))
    if limits is None:
        return True
    min_count, min_dialogue, count_limit = limits
    count = character_count(record["text"])
    if count_limit is not None and count > count_limit:
        return False
    return count >= min_count or record.get("dialogue_turn_count", 0) >= min_dialogue


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
    policy = record.get("selection_policy")
    if isinstance(policy, str) and policy.startswith(PUSH_POLICY_V3_PREFIX) and selection_limits(policy) is None:
        errors.append("selection_policy 格式不正确")
    if selection_limits(policy) is not None:
        turns = record.get("dialogue_turn_count")
        if not isinstance(turns, int) or turns < 0:
            errors.append("dialogue_turn_count 必须是非负整数")
        elif not meets_push_threshold(record):
            limits = selection_limits(policy)
            assert limits is not None
            min_count, min_dialogue, count_limit = limits
            if count_limit is not None and character_count(record["text"]) > count_limit:
                errors.append(f"超过字数上限：原文字数需≤{count_limit}")
            else:
                errors.append(f"不满足推送门槛：原文字数需≥{min_count}，或对话轮数需≥{min_dialogue}")
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


def load_candidates(path: Path) -> list[dict[str, Any]]:
    """Load either a records envelope or a bare candidate list."""
    with path.open("r", encoding="utf-8") as handle:
        incoming = json.load(handle)
    candidates = incoming["records"] if isinstance(incoming, dict) else incoming
    if not isinstance(candidates, list):
        raise ValueError("输入必须是 records 数组或包含 records 的对象。")
    if not all(isinstance(candidate, dict) for candidate in candidates):
        raise ValueError("候选项必须都是对象。")
    return candidates


def prepare_candidate(
    candidate: dict[str, Any],
    state: str | None = None,
    min_count: int = DEFAULT_MIN_COUNT,
    min_dialogue: int = DEFAULT_MIN_DIALOGUE,
    count_limit: int = DEFAULT_COUNT_LIMIT,
) -> dict[str, Any]:
    """Copy a candidate and calculate metadata whenever its core fields exist."""
    item = dict(candidate)
    if state:
        item["state"] = state
    item.setdefault("selection_policy", make_selection_policy(min_count, min_dialogue, count_limit))
    if isinstance(item.get("text"), str) and isinstance(item.get("semantic_signature"), dict):
        return enrich(item)
    return item


def screen_candidates(
    candidates: list[dict[str, Any]],
    records: list[dict[str, Any]],
    limit: int | None = None,
    state: str | None = None,
    min_count: int = DEFAULT_MIN_COUNT,
    min_dialogue: int = DEFAULT_MIN_DIALOGUE,
    count_limit: int = DEFAULT_COUNT_LIMIT,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Return accepted candidates and rejection reasons without changing the registry.

    Accepted items are added to the in-memory comparison set. This makes duplicate
    checks apply both to archived jokes and to other accepted items in this batch.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit 必须至少为 1。")
    validate_policy_limits(min_count, min_dialogue, count_limit)

    accepted: list[dict[str, Any]] = []
    rejected: list[tuple[str, str]] = []
    comparison_records = list(records)
    seen_ids = {record.get("id") for record in records}
    for raw_candidate in candidates:
        candidate = prepare_candidate(raw_candidate, state, min_count, min_dialogue, count_limit)
        item_id = str(candidate.get("id", "<无 id>"))
        if candidate.get("id") in seen_ids:
            rejected.append((item_id, "id 重复"))
            continue
        seen_ids.add(candidate.get("id"))

        errors = validate_shape(candidate)
        reason = duplicate_reason(candidate, comparison_records) if not errors else None
        if errors or reason:
            rejected.append((item_id, "; ".join(errors) if errors else reason))
            continue
        if limit is not None and len(accepted) >= limit:
            rejected.append((item_id, "已达到本批接纳上限"))
            continue
        accepted.append(candidate)
        comparison_records.append(candidate)
    return accepted, rejected


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
    qualified = sum(selection_limits(record.get("selection_policy")) is not None and meets_push_threshold(record) for record in records)
    print(f"校验通过：{len(records)} 条；状态 {dict(states)}；长度 {min(lengths)}–{max(lengths)} 字；推送规则合格 {qualified} 条。")
    return 0


def command_add(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    candidates = load_candidates(args.input)
    min_count = getattr(args, "min_count", DEFAULT_MIN_COUNT)
    min_dialogue = getattr(args, "min_dialogue", DEFAULT_MIN_DIALOGUE)
    count_limit = getattr(args, "count_limit", DEFAULT_COUNT_LIMIT)
    accepted, rejected = screen_candidates(
        candidates,
        registry["records"],
        state=args.state,
        min_count=min_count,
        min_dialogue=min_dialogue,
        count_limit=count_limit,
    )
    if accepted:
        registry["records"].extend(accepted)
        save_registry(args.registry, registry)
    print(f"已入库 {len(accepted)} 条；拦截 {len(rejected)} 条。")
    for item_id, reason in rejected:
        print(f"- {item_id}：{reason}")
    return 1 if rejected else 0


def command_screen(args: argparse.Namespace) -> int:
    """Screen a batch and optionally materialize only the accepted subset."""
    registry = load_registry(args.registry)
    candidates = load_candidates(args.input)
    min_count = getattr(args, "min_count", DEFAULT_MIN_COUNT)
    min_dialogue = getattr(args, "min_dialogue", DEFAULT_MIN_DIALOGUE)
    count_limit = getattr(args, "count_limit", DEFAULT_COUNT_LIMIT)
    accepted, rejected = screen_candidates(
        candidates,
        registry["records"],
        args.limit,
        args.state,
        min_count,
        min_dialogue,
        count_limit,
    )
    if args.accepted_output:
        args.accepted_output.parent.mkdir(parents=True, exist_ok=True)
        with args.accepted_output.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump({"records": accepted}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(f"筛选完成：接纳 {len(accepted)} 条；拦截 {len(rejected)} 条。")
    for item_id, reason in rejected:
        print(f"- {item_id}：{reason}")
    if args.accepted_output:
        print(f"已写入待入库候选：{args.accepted_output}")
    return 0


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
    if args.require is not None and len(records) < args.require:
        print(f"批次仅有 {len(records)} 条，未达到要求的 {args.require} 条。", file=sys.stderr)
        return 1
    if not getattr(args, "no_emoji", False):
        ensure_utf8_stdout()
    print(f"# 每日一笑｜{batch}\n")
    for index, record in enumerate(records[: args.limit], start=1):
        meta = enrich(record)
        print(f"## {index}. {record['twist_type']}｜{'、'.join(record['tags'])}")
        rendered_text = record["text"].strip()
        if not getattr(args, "no_emoji", False):
            rendered_text = decorate_text(rendered_text)
        print(rendered_text)
        print(f"\n{source_markdown(record)} · {meta['character_count']} 字\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日一笑档案、去重和展示工具")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY, help="档案 JSON 路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="校验字段、安全边界和三层去重")
    screen = sub.add_parser("screen", help="无副作用筛选候选，可输出通过项供后续入库")
    screen.add_argument("input", type=Path, help="候选 JSON 文件")
    screen.add_argument("--limit", type=int, help="本批最多接纳的条数")
    screen.add_argument("--state", choices=["candidate", "shown", "adopted"], help="覆盖通过项状态")
    screen.add_argument("--accepted-output", type=Path, help="写入通过候选的临时 JSON 路径")
    add_screen_policy_arguments(screen)
    add = sub.add_parser("add", help="将联网筛出的候选 JSON 入库")
    add.add_argument("input", type=Path, help="候选 JSON 文件")
    add.add_argument("--state", choices=["candidate", "shown", "adopted"], help="覆盖候选状态")
    add_screen_policy_arguments(add)
    render = sub.add_parser("render", help="按批次输出已展示笑话")
    render.add_argument("--batch", help="批次 ID；省略时取最近批次")
    render.add_argument("--limit", type=int, default=10, help="最多输出条数")
    render.add_argument("--require", type=int, help="要求批次至少包含的条数")
    render.add_argument("--no-emoji", action="store_true", help="严格输出档案原文，不添加展示层 Emoji")
    sub.add_parser("refresh", help="将长度、行数和指纹写回手工导入的档案")
    return parser


def add_screen_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--count", "-count", dest="min_count", type=non_negative_int, default=DEFAULT_MIN_COUNT, help=f"最低非空白字符数，默认 {DEFAULT_MIN_COUNT}")
    parser.add_argument("--topic", "-topic", dest="min_dialogue", type=non_negative_int, default=DEFAULT_MIN_DIALOGUE, help=f"最低对话轮数，默认 {DEFAULT_MIN_DIALOGUE}")
    parser.add_argument("--countLimit", "-countLimit", dest="count_limit", type=positive_int, default=DEFAULT_COUNT_LIMIT, help=f"最大非空白字符数，默认 {DEFAULT_COUNT_LIMIT}")


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {"screen", "add"}:
        try:
            validate_policy_limits(args.min_count, args.min_dialogue, args.count_limit)
        except ValueError as error:
            parser.error(str(error))
    if args.command == "validate":
        return command_validate(args)
    if args.command == "add":
        return command_add(args)
    if args.command == "refresh":
        return command_refresh(args)
    if args.command == "screen":
        return command_screen(args)
    return command_render(args)


if __name__ == "__main__":
    raise SystemExit(main())
