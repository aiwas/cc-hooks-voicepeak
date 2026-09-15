#!/usr/bin/env python3
"""テスト用の常駐プレイヤ代替.

``PowershellPlayer`` が期待する標準入力プロトコル (PID 行を返し、1 行ずつ
wav のパスを受け取り、``__QUIT__`` で終わる) を実装する。PowerShell 側の
引数はすべて無視するので、``executable`` にこのスクリプトを渡せばそのまま
差し替えられる。

環境変数
    FAKE_PLAYER_LOG    受け取った行を追記する先
    FAKE_PLAYER_MODE   "normal" (既定) / "silent" (PID を返さない) /
                       "die" (即終了)
"""

from __future__ import annotations

import os
import sys


def record(line: str) -> None:
    path = os.environ.get("FAKE_PLAYER_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    mode = os.environ.get("FAKE_PLAYER_MODE", "normal")
    if mode == "die":
        return 1

    if mode != "silent":
        print(f"PID {os.getpid()}", flush=True)

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        if line == "__QUIT__":
            record("__QUIT__")
            break
        record(line)
        print(f"PLAYED {line}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
