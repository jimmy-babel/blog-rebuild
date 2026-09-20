#!/usr/bin/env python3
"""Choose the deterministic fallback character for an ambiguous article image."""

from __future__ import annotations

import argparse
import sys


DEFAULT_CHARACTER = "girl"


def choose_character(url: str, index: int) -> str:
    if index < 1:
        raise ValueError("图片序号必须从 1 开始")
    return DEFAULT_CHARACTER


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--index", required=True, type=int)
    args = parser.parse_args()
    print(choose_character(args.url, args.index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
