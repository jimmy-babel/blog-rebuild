#!/usr/bin/env python3
"""Choose a deterministic fallback emotion for an ambiguous article image."""

from __future__ import annotations

import argparse
import hashlib
import sys


FALLBACK_EMOTIONS = ("喜", "乐", "通用")


def choose_emotion(url: str, index: int) -> str:
    if index < 1:
        raise ValueError("图片序号必须从 1 开始")
    digest = hashlib.sha256(f"{url}#{index}".encode("utf-8")).digest()
    return FALLBACK_EMOTIONS[int.from_bytes(digest[:8], "big") % len(FALLBACK_EMOTIONS)]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--index", required=True, type=int)
    args = parser.parse_args()
    print(choose_emotion(args.url, args.index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
