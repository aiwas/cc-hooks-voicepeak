#!/usr/bin/env python3
"""テスト用の voicepeak.exe 代替.

本物と同じオプションを受け取り、140 文字を超えたらエラーにして wav を出さない。
呼び出し内容は ``FAKE_VOICEPEAK_LOG`` が指す JSONL に追記する。

環境変数
    FAKE_VOICEPEAK_LIMIT       文字数上限 (既定 140)
    FAKE_VOICEPEAK_LOG         呼び出し内容の記録先 (JSONL)
    FAKE_VOICEPEAK_HANG        指定秒だけ眠る (タイムアウト検証用)
    FAKE_VOICEPEAK_ENCODING    標準出力の文字コード (cp932 の復号を検証)
    FAKE_VOICEPEAK_FAIL_MODE   "say" / "text_file" — そのモードだけ失敗させる
    FAKE_VOICEPEAK_LOCK        同時起動を検出するためのロックファイル
"""

from __future__ import annotations

import json
import os
import sys
import time
import wave

LIMIT = int(os.environ.get("FAKE_VOICEPEAK_LIMIT", "140"))


def emit(text: str, stream=None) -> None:
    """``FAKE_VOICEPEAK_ENCODING`` に従って出力する."""
    stream = stream or sys.stdout
    encoding = os.environ.get("FAKE_VOICEPEAK_ENCODING")
    if not encoding:
        print(text, file=stream)
        return
    stream.buffer.write((text + "\n").encode(encoding, errors="replace"))
    stream.buffer.flush()


def enter_lock() -> bool:
    """ロックを取れたら True. 既に誰かが実行中なら False."""
    path = os.environ.get("FAKE_VOICEPEAK_LOCK")
    if not path:
        return True
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        log_call({"concurrent": True})
        return False
    os.close(handle)
    return True


def leave_lock() -> None:
    path = os.environ.get("FAKE_VOICEPEAK_LOCK")
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def log_call(record: dict) -> None:
    path = os.environ.get("FAKE_VOICEPEAK_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_wav(path: str, seconds: float) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    framerate = 48000
    with wave.open(path, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(framerate)
        writer.writeframes(b"\x00\x00" * int(framerate * seconds))


def main(argv: list[str]) -> int:
    args = argv[1:]
    if "--help" in args or "-h" in args:
        print("usage: voicepeak [-s TEXT] [-t FILE] [-o OUT] [-n NARRATOR] [-e EXPR]")
        return 0
    if "--list-narrator" in args:
        emit("Fake Narrator A")
        emit("Fake Narrator B")
        return 0
    if "--list-emotion" in args:
        emit("happy")
        emit("sad")
        return 0

    values: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-s", "--say", "-t", "--text", "-o", "--out", "-n", "--narrator",
                     "-e", "--emotion", "--speed", "--pitch"):
            if index + 1 >= len(args):
                print(f"missing value for {token}", file=sys.stderr)
                return 2
            values[token.lstrip("-")] = args[index + 1]
            index += 2
            continue
        print(f"unknown option: {token}", file=sys.stderr)
        return 2

    text = values.get("s") or values.get("say")
    if text is None:
        text_file = values.get("t") or values.get("text")
        if not text_file:
            print("no text given", file=sys.stderr)
            return 2
        with open(text_file, encoding="utf-8") as handle:
            text = handle.read()

    out = values.get("o") or values.get("out") or "output.wav"
    mode = "text_file" if ("t" in values or "text" in values) else "say"

    if not enter_lock():
        print("another voicepeak is running", file=sys.stderr)
        return 1
    try:
        hang = float(os.environ.get("FAKE_VOICEPEAK_HANG", "0") or 0)
        if hang:
            time.sleep(hang)

        log_call(
            {
                "text": text,
                "length": len(text),
                "out": out,
                "narrator": values.get("n") or values.get("narrator"),
                "emotion": values.get("e") or values.get("emotion"),
                "speed": values.get("speed"),
                "pitch": values.get("pitch"),
                "mode": mode,
            }
        )

        if os.environ.get("FAKE_VOICEPEAK_FAIL_MODE") == mode:
            print(f"failed on purpose ({mode})", file=sys.stderr)
            return 1
        if len(text) > LIMIT:
            print(f"Text is too long: {len(text)} > {LIMIT}", file=sys.stderr)
            return 1
        if not text.strip():
            print("Text is empty", file=sys.stderr)
            return 1

        write_wav(out, max(0.05, len(text) * 0.01))
        return 0
    finally:
        leave_lock()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
