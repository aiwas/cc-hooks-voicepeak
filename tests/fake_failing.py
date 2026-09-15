#!/usr/bin/env python3
"""常に異常終了する voicepeak.exe 代替.

終了コードを検査していないと、このエラーメッセージが声の一覧として
そのまま返ってしまう。
"""

from __future__ import annotations

import sys


def main() -> int:
    print("License check failed: could not reach the activation server", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
