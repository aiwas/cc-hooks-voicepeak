"""CLI と hook 経路のテスト (子プロセスとして実行する)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fake_voicepeak.py"
FAKE_PLAYER = REPO / "tests" / "fake_player.py"
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

    def wait_for_calls(self, count: int = 1, timeout: float = 20.0):
        """デタッチした読み上げプロセスが合成を終えるまで待つ."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            calls = self.recorded_calls()
            if len(calls) >= count:
                return calls
            time.sleep(0.05)
        return self.recorded_calls()


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

    def test_split_json_empty_input_exits_nonzero(self):
        proc = self.run_cli("split", "--stdin", "--json", stdin="   ")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["blocks"], [])

    def test_missing_file_reports_error_without_traceback(self):
        proc = self.run_cli("split", "-f", str(self.tmp / "no-such.md"))
        self.assertEqual(proc.returncode, 1)
        stderr = proc.stderr.decode("utf-8")
        self.assertIn("ファイルを読み込めません", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_piping_to_head_is_not_an_error(self):
        # `cc-voicepeak split | head` で Broken pipe を出さない
        proc = subprocess.run(
            f"{sys.executable} -m cc_voicepeak split --stdin | head -2",
            input=("一つ目の文です。" * 60).encode("utf-8"),
            capture_output=True,
            cwd=str(REPO),
            env=self.env,
            shell=True,
            timeout=120,
        )
        self.assertNotIn("Broken pipe", proc.stderr.decode("utf-8"))

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

    # -- --player / --concat ------------------------------------------------
    #
    # select_player("powershell") は find_powershell() 経由で PATH を引くだけ
    # なので、PATH の先頭に powershell.exe の名前で代役を置けば、本物を呼ばずに
    # 常駐プレイヤの経路（PID 行 / wav パス / __QUIT__）をそのまま通せる。

    PLAYED_TEXT = "再生経路の確認をしています。" * 30

    def install_fake_powershell(self) -> Path:
        played = self.tmp / "played.log"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shim = bin_dir / "powershell.exe"
        shim.write_text(
            "#!/usr/bin/env bash" + NL + f'exec "{sys.executable}" "{FAKE_PLAYER}" "$@"' + NL,
            encoding="utf-8",
        )
        shim.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_PLAYER_LOG"] = str(played)
        return played

    def played_paths(self, played: Path):
        if not played.exists():
            return []
        return [
            line
            for line in played.read_text(encoding="utf-8").splitlines()
            if line.strip() and line != "__QUIT__"
        ]

    def block_count(self, text: str) -> int:
        """同じ設定での分割数を数える.

        合成回数 (``recorded_calls()``) は同じ文面がキャッシュに当たると減るので、
        再生本数の期待値には使えない。
        """
        proc = self.run_cli("split", "--stdin", "--json", stdin=text, check=True)
        return len(json.loads(proc.stdout.decode("utf-8"))["blocks"])

    def test_player_option_selects_the_backend(self):
        played = self.install_fake_powershell()
        expected = self.block_count(self.PLAYED_TEXT)
        self.assertGreater(expected, 1)

        self.run_cli(
            "speak", "--stdin", "--player", "powershell", stdin=self.PLAYED_TEXT, check=True
        )
        paths = self.played_paths(played)
        # 連結しない場合はブロックごとに 1 本ずつ流し込まれる
        self.assertEqual(len(paths), expected)
        for path in paths:
            self.assertTrue(path.endswith(".wav"), path)

    def test_concat_sends_a_single_file_to_the_player(self):
        played = self.install_fake_powershell()
        self.assertGreater(self.block_count(self.PLAYED_TEXT), 1)

        self.run_cli(
            "speak", "--stdin", "--player", "powershell", "--concat",
            stdin=self.PLAYED_TEXT, check=True,
        )
        # 複数ブロックでも、再生は連結した 1 本だけ
        self.assertEqual(len(self.played_paths(played)), 1)

    def test_player_option_overrides_the_config_file(self):
        played = self.install_fake_powershell()
        # 環境変数は設定ファイルより後に重なるので、外さないと設定が埋もれる
        self.env.pop("CC_VOICEPEAK_PLAYER", None)
        self.write_project_config({"player": {"backend": "powershell"}})

        # まず設定ファイルどおりに再生されることを確かめる (空振りの検出)
        self.run_cli("speak", "--stdin", stdin="設定ファイルの指定で再生されます。", check=True)
        self.assertEqual(len(self.played_paths(played)), 1)

        played.unlink()
        self.run_cli(
            "speak", "--stdin", "--player", "none", stdin="引数のほうが優先されます。", check=True
        )
        self.assertTrue(self.recorded_calls())  # 合成はされている
        self.assertEqual(self.played_paths(played), [])


class HookCommandTest(CliTestCase):
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
        self.assertTrue(self.wait_for_calls(), "別プロセスでの合成が行われていない")

    def test_detached_hook_passes_voice_options(self):
        path = self.transcript("声の指定を引き継ぐか確認します。")
        payload = {
            "hook_event_name": "Stop",
            "session_id": "voice-test",
            "transcript_path": str(path),
        }
        self.run_cli(
            "hook",
            "-n",
            "Fake Narrator B",
            "--speed",
            "120",
            stdin=json.dumps(payload),
            check=True,
        )
        calls = self.wait_for_calls()
        self.assertTrue(calls, "別プロセスでの合成が行われていない")
        self.assertEqual(calls[0]["narrator"], "Fake Narrator B")
        self.assertEqual(calls[0]["speed"], "120")

    def test_detached_hook_passes_config_option(self):
        extra = self.tmp / "extra.json"
        extra.write_text(
            json.dumps({"voicepeak": {"narrator": "Fake Narrator B"}}), encoding="utf-8"
        )
        path = self.transcript("設定ファイルの引き継ぎを確認します。")
        payload = {
            "hook_event_name": "Stop",
            "session_id": "config-test",
            "transcript_path": str(path),
        }
        self.run_cli(
            "--config", str(extra), "hook", stdin=json.dumps(payload), check=True
        )
        calls = self.wait_for_calls()
        self.assertTrue(calls, "別プロセスでの合成が行われていない")
        self.assertEqual(calls[0]["narrator"], "Fake Narrator B")


class HookConfigTest(CliTestCase):
    def test_min_chars_skips_short_text(self):
        self.write_project_config({"hook": {"min_chars": 20}})
        payload = {"hook_event_name": "Notification", "message": "短い"}
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        self.assertEqual(self.recorded_calls(), [])

    def test_prefix_and_suffix_are_added(self):
        self.write_project_config({"hook": {"prefix": "まもなく、", "suffix": "以上です。"}})
        path = self.transcript("処理が完了しました。")
        payload = {"hook_event_name": "Stop", "transcript_path": str(path)}
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        spoken = "".join(call["text"] for call in self.recorded_calls())
        self.assertTrue(spoken.startswith("まもなく、"), spoken)
        self.assertTrue(spoken.endswith("以上です。"), spoken)

    def test_notification_prefix_is_added(self):
        self.write_project_config({"hook": {"notification_prefix": "お知らせ。"}})
        payload = {"hook_event_name": "Notification", "message": "許可が必要です"}
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        self.assertTrue(self.recorded_calls()[0]["text"].startswith("お知らせ。"))

    def test_subagent_stop_is_skipped_by_default(self):
        path = self.transcript("サブエージェントの結果です。")
        payload = {"hook_event_name": "SubagentStop", "transcript_path": str(path)}
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        self.assertEqual(self.recorded_calls(), [])

    def test_subagent_stop_is_read_when_enabled(self):
        self.write_project_config({"hook": {"subagent": True}})
        path = self.transcript("サブエージェントの結果です。")
        payload = {"hook_event_name": "SubagentStop", "transcript_path": str(path)}
        self.run_cli("hook", "--sync", stdin=json.dumps(payload), check=True)
        self.assertTrue(self.recorded_calls())

    def test_detach_false_speaks_before_returning(self):
        self.write_project_config({"hook": {"detach": False}})
        path = self.transcript("同期で読み上げます。")
        payload = {"hook_event_name": "Stop", "transcript_path": str(path)}
        # --sync を付けなくても hook.detach が false なら待つ
        self.run_cli("hook", stdin=json.dumps(payload), check=True)
        self.assertTrue(self.recorded_calls())


class SafePathTest(CliTestCase):
    """cwd に置かれたおとりの cc_voicepeak/ が本物より先に import されないこと.

    `python -m` は cwd を sys.path[0] に入れるため、Claude Code が hook を
    プロジェクトディレクトリで起動すると、そこに置かれた cc_voicepeak/ が
    PYTHONPATH の本物より先に見つかる。
    """

    def setUp(self) -> None:
        super().setUp()
        self.decoy = self.tmp / "decoy"
        self.marker = self.tmp / "decoy-ran"
        package = self.decoy / "cc_voicepeak"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(
            NL.join(
                [
                    "import os, pathlib",
                    "pathlib.Path(os.environ['DECOY_MARKER']).write_text('decoy')",
                    "print('DECOY')",
                ]
            ),
            encoding="utf-8",
        )
        self.env["DECOY_MARKER"] = str(self.marker)
        self.env.pop("PYTHONSAFEPATH", None)

    def run_from_decoy(self, command, stdin: str = ""):
        return subprocess.run(
            command,
            input=stdin.encode("utf-8"),
            capture_output=True,
            cwd=str(self.decoy),
            env=self.env,
            timeout=120,
        )

    def stop_payload(self, session: str) -> str:
        path = self.transcript("おとりの確認です。")
        return json.dumps(
            {"hook_event_name": "Stop", "session_id": session, "transcript_path": str(path)}
        )

    def test_decoy_is_picked_up_without_protection(self):
        # 前提の確認: 何もしなければ cwd のおとりが動く
        proc = self.run_from_decoy([sys.executable, "-m", "cc_voicepeak", "--version"])
        self.assertIn("DECOY", proc.stdout.decode("utf-8"))
        self.assertTrue(self.marker.exists())

    def test_launcher_ignores_decoy_in_cwd(self):
        launcher = REPO / "bin" / "cc-voicepeak"
        proc = self.run_from_decoy([str(launcher), "--version"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("cc-voicepeak", proc.stdout.decode("utf-8"))
        self.assertNotIn("DECOY", proc.stdout.decode("utf-8"))
        self.assertFalse(self.marker.exists())

    def test_detached_child_ignores_decoy_in_cwd(self):
        # 親は -P で守り、環境変数は渡さない。子が守られるのは
        # spawn_detached() 自身が -P を付けている場合だけ
        proc = self.run_from_decoy(
            [sys.executable, "-P", "-m", "cc_voicepeak", "hook"],
            stdin=self.stop_payload("decoy-detach"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertTrue(self.wait_for_calls(), "別プロセスでの合成が行われていない")
        self.assertFalse(self.marker.exists(), "デタッチした子がおとりを実行した")


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

    def test_hook_with_misplaced_global_option_exits_zero(self):
        # `hook -v` (グローバルオプションをサブコマンドの後ろに置く順序違い) は
        # argparse が exit 2 にするが、Stop hook の exit 2 は「停止のブロック」になる
        payload = {"hook_event_name": "Notification", "message": "テスト"}
        proc = self.run_cli("hook", "-v", stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("unrecognized arguments", proc.stderr.decode("utf-8"))
        self.assertFalse(self.calls.exists())

    def test_hook_with_unknown_option_exits_zero(self):
        proc = self.run_cli("hook", "--bogus", stdin="{}")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_argument_errors_outside_hook_keep_argparse_exit_code(self):
        self.assertEqual(self.run_cli("speak", "--bogus").returncode, 2)
        self.assertEqual(self.run_cli().returncode, 2)


class ExeLockTest(CliTestCase):
    def test_voicepeak_is_not_launched_concurrently(self):
        """ExeLock がホスト全体で直列化していることを 2 プロセスで確かめる."""
        env = {
            **self.env,
            "FAKE_VOICEPEAK_LOCK": str(self.tmp / "exe.lock"),
            "FAKE_VOICEPEAK_HANG": "0.3",
        }
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "cc_voicepeak", "speak", "--stdin"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(REPO),
                env=env,
            )
            for _ in range(2)
        ]
        for index, proc in enumerate(procs):
            proc.communicate(f"{index}番目の同時起動の確認です。".encode("utf-8"), timeout=120)

        calls = self.recorded_calls()
        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse(
            [call for call in calls if call.get("concurrent")],
            "voicepeak.exe が同時起動している",
        )


class MiscCommandTest(CliTestCase):
    def test_install_hook_outputs_valid_json(self):
        proc = self.run_cli("install-hook", check=True)
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertIn("Stop", payload["hooks"])
        self.assertIn("Notification", payload["hooks"])
        entry = payload["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(entry["type"], "command")
        self.assertIn("cc-voicepeak", entry["command"])

    def test_install_hook_installed_form(self):
        proc = self.run_cli("install-hook", "--installed", check=True)
        payload = json.loads(proc.stdout.decode("utf-8"))
        entry = payload["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(entry["command"], "cc-voicepeak hook")

    def test_install_hook_custom_command_and_events(self):
        proc = self.run_cli(
            "install-hook", "--command", "/opt/x/cc-voicepeak hook", "--events", "Stop", check=True
        )
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(list(payload["hooks"]), ["Stop"])
        self.assertEqual(
            payload["hooks"]["Stop"][0]["hooks"][0]["command"], "/opt/x/cc-voicepeak hook"
        )

    def test_install_hook_rejects_empty_events(self):
        proc = self.run_cli("install-hook", "--events", " , ")
        self.assertEqual(proc.returncode, 1)

    def test_narrators_uses_exe(self):
        proc = self.run_cli("narrators", check=True)
        self.assertIn("Fake Narrator A", proc.stdout.decode("utf-8"))

    def test_emotions_uses_exe(self):
        proc = self.run_cli("emotions", "Fake Narrator A", check=True)
        self.assertIn("happy", proc.stdout.decode("utf-8"))

    def test_check_synth_runs_synthesis(self):
        proc = self.run_cli("check", "--synth")
        self.assertIn("合成テスト", proc.stdout.decode("utf-8"))
        self.assertTrue(self.recorded_calls())

    def test_verbose_reports_summary(self):
        proc = self.run_cli("-v", "speak", "--stdin", stdin="進捗の確認です。", check=True)
        self.assertIn("ブロック", proc.stderr.decode("utf-8"))

    def test_check_warns_about_unknown_keys(self):
        path = Path(self.env["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"voicepeak": {"narator": "誤字"}}), encoding="utf-8"
        )
        proc = self.run_cli("check")
        output = proc.stdout.decode("utf-8")
        self.assertIn("設定キー", output)
        self.assertIn("voicepeak.narator", output)

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
