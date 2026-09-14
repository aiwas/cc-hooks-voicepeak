"""CLI と hook 経路のテスト (子プロセスとして実行する)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fake_voicepeak.py"
NL = chr(10)


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-cli-"))
        self.calls = self.tmp / "calls.jsonl"
        self.env = {
            **os.environ,
            "PYTHONPATH": str(REPO),
            "FAKE_VOICEPEAK_LOG": str(self.calls),
            "CC_VOICEPEAK_EXE": str(FAKE),
            "CC_VOICEPEAK_TEMP": str(self.tmp / "work"),
            "CC_VOICEPEAK_BRIDGE": "local",
            "CC_VOICEPEAK_PLAYER": "none",
            "CC_VOICEPEAK_LOG_LEVEL": "off",
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_RUNTIME_DIR": str(self.tmp / "run"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "CLAUDE_PROJECT_DIR": str(self.tmp / "project"),
        }
        self.env.pop("CC_VOICEPEAK_CONFIG", None)
        (self.tmp / "run").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, stdin: str = "", check: bool = False):
        proc = subprocess.run(
            [sys.executable, "-m", "cc_voicepeak", *args],
            input=stdin.encode("utf-8"),
            capture_output=True,
            cwd=str(REPO),
            env=self.env,
            timeout=120,
        )
        if check:
            self.assertEqual(
                proc.returncode,
                0,
                proc.stdout.decode() + proc.stderr.decode(),
            )
        return proc

    def write_project_config(self, data: dict) -> Path:
        path = Path(self.env["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class SplitCommandTest(CliTestCase):
    def test_split_json_output(self):
        text = "一つ目の文です。" * 40
        proc = self.run_cli("split", "--stdin", "--json", stdin=text, check=True)
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(payload["limit"], 140)
        self.assertGreater(len(payload["blocks"]), 1)
        for block in payload["blocks"]:
            self.assertLessEqual(block["width"], 140)

    def test_split_respects_limit_option(self):
        proc = self.run_cli(
            "split", "--stdin", "--json", "--limit", "30", stdin="短い文です。" * 20, check=True
        )
        payload = json.loads(proc.stdout.decode("utf-8"))
        for block in payload["blocks"]:
            self.assertLessEqual(block["width"], 30)

    def test_split_text_argument(self):
        proc = self.run_cli("split", "こんにちは。", check=True)
        self.assertIn("こんにちは。", proc.stdout.decode("utf-8"))


class SpeakCommandTest(CliTestCase):
    def test_speak_invokes_exe_per_block(self):
        text = "処理が完了しました。" * 40
        self.run_cli("speak", "--stdin", stdin=text, check=True)
        calls = self.recorded_calls()
        self.assertGreater(len(calls), 1)
        for call in calls:
            self.assertLessEqual(call["length"], 140)

    def test_dry_run_does_not_invoke_exe(self):
        self.run_cli("speak", "--stdin", "--dry-run", stdin="確認します。", check=True)
        self.assertEqual(self.recorded_calls(), [])

    def test_out_file_is_written(self):
        dest = self.tmp / "speech.wav"
        self.run_cli(
            "speak", "--stdin", "-o", str(dest), stdin="保存の確認です。" * 30, check=True
        )
        self.assertTrue(dest.exists())
        self.assertGreater(dest.stat().st_size, 44)

    def test_empty_input_fails_gracefully(self):
        proc = self.run_cli("speak", "--stdin", stdin="   ")
        self.assertEqual(proc.returncode, 1)


class HookCommandTest(CliTestCase):
    def transcript(self, text: str) -> Path:
        path = self.tmp / "transcript.jsonl"
        entries = [
            {"type": "user", "message": {"role": "user", "content": "やって"}},
            {
                "type": "assistant",
                "isSidechain": False,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            },
        ]
        path.write_text(
            NL.join(json.dumps(entry, ensure_ascii=False) for entry in entries),
            encoding="utf-8",
        )
        return path

    def test_stop_event_reads_last_assistant_message(self):
        path = self.transcript("## 完了" + NL + NL + "テストは全部で二十三件、すべて成功しました。")
        payload = {
            "hook_event_name": "Stop",
            "session_id": "abc123",
            "transcript_path": str(path),
            "stop_hook_active": False,
        }
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        spoken = " ".join(call["text"] for call in self.recorded_calls())
        self.assertIn("二十三件", spoken)
        self.assertNotIn("##", spoken)

    def test_long_message_is_split_into_multiple_calls(self):
        # 同一文はキャッシュで EXE 起動が省かれるので、毎文の内容を変える
        body = "".join(f"{i}番目の処理が完了しました。" for i in range(60))
        path = self.transcript(body)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "abc123",
            "transcript_path": str(path),
        }
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        calls = self.recorded_calls()
        self.assertGreater(len(calls), 3)
        for call in calls:
            self.assertLessEqual(call["length"], 140)

    def test_notification_event_reads_message(self):
        payload = {"hook_event_name": "Notification", "message": "許可が必要です"}
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        self.assertIn("許可が必要です", self.recorded_calls()[0]["text"])

    def test_unrelated_event_is_skipped(self):
        payload = {"hook_event_name": "PreToolUse", "message": "無視される"}
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        self.assertEqual(self.recorded_calls(), [])

    def test_missing_transcript_exits_zero(self):
        payload = {"hook_event_name": "Stop", "transcript_path": "/nonexistent.jsonl"}
        proc = self.run_cli("hook", "--sync", stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.recorded_calls(), [])

    def test_empty_stdin_exits_zero(self):
        proc = self.run_cli("hook", "--sync", stdin="")
        self.assertEqual(proc.returncode, 0)

    def test_broken_exe_does_not_break_claude_code(self):
        env = dict(self.env)
        env["CC_VOICEPEAK_EXE"] = "/nonexistent/voicepeak.exe"
        proc = subprocess.run(
            [sys.executable, "-m", "cc_voicepeak", "hook", "--sync"],
            input=json.dumps(
                {"hook_event_name": "Notification", "message": "テスト"}
            ).encode("utf-8"),
            capture_output=True,
            cwd=str(REPO),
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_detached_hook_returns_immediately(self):
        path = self.transcript("非同期の確認です。" * 5)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "detach-test",
            "transcript_path": str(path),
        }
        proc = self.run_cli("hook", stdin=json.dumps(payload), check=True)
        self.assertEqual(proc.stdout.decode().strip(), "")
        # 子プロセスが読み上げを終えるのを待つ
        for _ in range(200):
            if self.recorded_calls():
                break
            import time

            time.sleep(0.05)
        self.assertTrue(self.recorded_calls(), "別プロセスでの合成が行われていない")


class ExitCodeTest(CliTestCase):
    def test_hook_survives_non_cc_voicepeak_exception(self):
        # hook.min_chars が設定ファイル由来だと int() が ValueError を送出する
        self.write_project_config({"hook": {"min_chars": "x"}})
        payload = {"hook_event_name": "Notification", "message": "テスト"}
        proc = self.run_cli("hook", "--sync", stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_hook_survives_config_error(self):
        self.write_project_config({"voicepeak": {"char_limit": 500}})
        payload = {"hook_event_name": "Notification", "message": "テスト"}
        proc = self.run_cli("hook", "--sync", stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_other_commands_report_failure_inside_project(self):
        # CLAUDE_PROJECT_DIR があっても hook 以外は終了コードを握り潰さない
        self.write_project_config({"voicepeak": {"char_limit": 500}})
        proc = self.run_cli("speak", "テスト")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("char_limit", proc.stderr.decode("utf-8"))


class MiscCommandTest(CliTestCase):
    def test_install_hook_outputs_valid_json(self):
        proc = self.run_cli("install-hook", check=True)
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertIn("Stop", payload["hooks"])
        self.assertIn("Notification", payload["hooks"])
        entry = payload["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(entry["type"], "command")
        self.assertIn("cc-voicepeak", entry["command"])

    def test_narrators_uses_exe(self):
        proc = self.run_cli("narrators", check=True)
        self.assertIn("Fake Narrator A", proc.stdout.decode("utf-8"))

    def test_check_reports_items(self):
        proc = self.run_cli("check")
        output = proc.stdout.decode("utf-8")
        self.assertIn("voicepeak", output)
        self.assertIn("ブリッジ", output)

    def test_check_notes(self):
        proc = self.run_cli("check", "--notes", check=True)
        self.assertIn("interop", proc.stdout.decode("utf-8"))

    def test_print_config(self):
        proc = self.run_cli("check", "--print-config", check=True)
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(payload["voicepeak"]["char_limit"], 140)

    def test_version(self):
        proc = self.run_cli("--version", check=True)
        self.assertIn("cc-voicepeak", proc.stdout.decode("utf-8"))
