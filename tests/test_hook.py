"""hook モジュールの単体テスト."""

from __future__ import annotations

import io
import os
import time
import unittest
from types import SimpleNamespace

from cc_voicepeak.hook import _write_stdin, read_payload


class TtyStream(io.StringIO):
    """tty を模した入力ストリーム."""

    def isatty(self) -> bool:
        return True


class ReadPayloadTest(unittest.TestCase):
    def test_json_object(self):
        self.assertEqual(read_payload(io.StringIO('{"hook_event_name": "Stop"}')),
                         {"hook_event_name": "Stop"})

    def test_plain_text_becomes_message(self):
        self.assertEqual(read_payload(io.StringIO("ただのテキスト")), {"message": "ただのテキスト"})

    def test_json_but_not_object(self):
        self.assertEqual(read_payload(io.StringIO("[1, 2]")), {"message": "[1, 2]"})

    def test_empty_input(self):
        self.assertEqual(read_payload(io.StringIO("   ")), {})

    def test_tty_returns_empty_without_reading(self):
        # 実際の tty では read() が返らない。手で叩いたときに固まらせない
        self.assertEqual(read_payload(TtyStream('{"message": "x"}')), {})


class WriteStdinTest(unittest.TestCase):
    def test_gives_up_when_pipe_is_full(self):
        read_fd, write_fd = os.pipe()
        handle = os.fdopen(write_fd, "wb", buffering=0)
        try:
            # 誰も読まないパイプへバッファ (通常 64KB) 超を書き込む
            started = time.monotonic()
            _write_stdin(SimpleNamespace(stdin=handle), b"x" * (1 << 20), timeout=0.3)
            self.assertLess(time.monotonic() - started, 5.0)
        finally:
            os.close(read_fd)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
