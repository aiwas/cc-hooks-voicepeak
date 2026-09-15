"""合成パイプラインのテスト (voicepeak.exe はダミーに差し替える)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from cc_voicepeak.bridge import LocalBridge
from cc_voicepeak.config import load_config
from cc_voicepeak.pipeline import prepare_blocks, speak, synth_to_file
from cc_voicepeak.player import NullPlayer
from cc_voicepeak.wavutil import wav_duration

FAKE = Path(__file__).parent / "fake_voicepeak.py"

LONG_TEXT = (
    "実装が完了しました。まず設定ファイルを読み込む処理を追加し、"
    "次にテキストを百四十文字以内へ分割する処理を実装しました。"
    "voicepeak は一回の起動で百四十文字までしか受け付けないため、"
    "長い文章は分割してから順番に音声へ変換する必要があります。"
    "テストはすべて成功しています。詳細は README を参照してください。"
) * 2


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-test-"))
        self.calls = self.tmp / "calls.jsonl"
        self._env = dict(os.environ)
        os.environ.update(
            {
                "FAKE_VOICEPEAK_LOG": str(self.calls),
                "CC_VOICEPEAK_TEMP": str(self.tmp / "work"),
                "CC_VOICEPEAK_BRIDGE": "local",
                "XDG_CONFIG_HOME": str(self.tmp / "config"),
                "XDG_RUNTIME_DIR": str(self.tmp / "run"),
                "CLAUDE_PROJECT_DIR": str(self.tmp / "project"),
            }
        )
        for name in ("CC_VOICEPEAK_CONFIG", "CC_VOICEPEAK_EXE", "CC_VOICEPEAK_NARRATOR"):
            os.environ.pop(name, None)
        self.bridge = LocalBridge(temp_root=self.tmp / "work")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, **overrides):
        base = {
            "voicepeak.exe": str(FAKE),
            "player.backend": "none",
            "log.level": "off",
        }
        base.update(overrides)
        return load_config(overrides=base)

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class SpeakTest(PipelineTestCase):
    def test_every_call_is_within_the_limit(self):
        report = speak(LONG_TEXT, self.config(), bridge=self.bridge)
        calls = self.recorded_calls()
        self.assertGreater(len(calls), 1, "分割されていない")
        self.assertEqual(len(calls), len(report.blocks))
        for call in calls:
            self.assertLessEqual(call["length"], 140, call["text"])
        self.assertEqual(report.ok_count, len(report.blocks))
        self.assertEqual(report.errors, [])

    def test_calls_are_serial_and_in_order(self):
        speak(LONG_TEXT, self.config(), bridge=self.bridge)
        calls = self.recorded_calls()
        outs = [Path(call["out"]).name for call in calls]
        self.assertEqual(outs, sorted(outs), "出力ファイルの順序が入れ替わっている")

    def test_voice_params_are_passed(self):
        speak(
            "短いテキストです。",
            self.config(
                **{
                    "voicepeak.narrator": "Fake Narrator A",
                    "voicepeak.emotion": "happy=50",
                    "voicepeak.speed": 120,
                    "voicepeak.pitch": -20,
                }
            ),
            bridge=self.bridge,
        )
        call = self.recorded_calls()[0]
        self.assertEqual(call["narrator"], "Fake Narrator A")
        self.assertEqual(call["emotion"], "happy=50")
        self.assertEqual(call["speed"], "120")
        self.assertEqual(call["pitch"], "-20")

    def test_player_receives_each_block(self):
        player = NullPlayer()
        report = speak(LONG_TEXT, self.config(), bridge=self.bridge, player=player)
        self.assertEqual(len(player.played), report.ok_count)

    def test_concat_mode_plays_single_file(self):
        player = NullPlayer()
        report = speak(
            LONG_TEXT,
            self.config(**{"player.concat": True}),
            bridge=self.bridge,
            player=player,
            keep_files=True,
        )
        self.assertEqual(len(player.played), 1)
        self.assertIsNotNone(report.output)
        self.assertGreater(wav_duration(report.output), 0.5)

    def test_cache_is_reused_on_second_run(self):
        cfg = self.config()
        speak(LONG_TEXT, cfg, bridge=self.bridge)
        first = len(self.recorded_calls())
        report = speak(LONG_TEXT, cfg, bridge=self.bridge)
        self.assertEqual(len(self.recorded_calls()), first, "キャッシュが使われていない")
        self.assertEqual(sum(1 for r in report.results if r.cached), len(report.blocks))

    def test_cache_can_be_disabled(self):
        cfg = self.config(**{"voicepeak.cache": False})
        speak("キャッシュ無効の確認です。", cfg, bridge=self.bridge)
        speak("キャッシュ無効の確認です。", cfg, bridge=self.bridge)
        self.assertEqual(len(self.recorded_calls()), 2)

    def test_text_file_mode(self):
        cfg = self.config(**{"voicepeak.input_mode": "text_file"})
        speak("ファイル渡しの確認です。", cfg, bridge=self.bridge)
        call = self.recorded_calls()[0]
        self.assertEqual(call["mode"], "text_file")
        self.assertEqual(call["text"], "ファイル渡しの確認です。")

    def test_falls_back_to_text_file_when_say_fails(self):
        # ダミー側の上限を下げ、-s では通らない長さにする
        os.environ["FAKE_VOICEPEAK_LIMIT"] = "5"
        try:
            report = speak("これは失敗するはずです。", self.config(), bridge=self.bridge)
        finally:
            os.environ.pop("FAKE_VOICEPEAK_LIMIT", None)
        modes = [call["mode"] for call in self.recorded_calls()]
        self.assertIn("text_file", modes, "text_file へのフォールバックが行われていない")
        self.assertTrue(report.errors)

    def test_cancel_stops_further_synthesis(self):
        state = {"count": 0}

        def should_cancel() -> bool:
            state["count"] += 1
            return state["count"] > 2

        report = speak(
            LONG_TEXT, self.config(), bridge=self.bridge, should_cancel=should_cancel
        )
        self.assertTrue(report.cancelled)
        self.assertLess(len(report.results), len(report.blocks))

    def test_empty_text_is_a_noop(self):
        report = speak("   ", self.config(), bridge=self.bridge)
        self.assertEqual(report.blocks, [])
        self.assertEqual(self.recorded_calls(), [])

    def test_markdown_is_not_read_aloud(self):
        text = chr(10).join(["## 見出し", "```python", "print('x')", "```", "本文です。"])
        speak(text, self.config(), bridge=self.bridge)
        spoken = " ".join(call["text"] for call in self.recorded_calls())
        self.assertNotIn("print", spoken)
        self.assertNotIn("##", spoken)
        self.assertIn("本文です。", spoken)

    def test_synth_to_file(self):
        dest = self.tmp / "out" / "speech.wav"
        report = synth_to_file(LONG_TEXT, self.config(), dest, bridge=self.bridge)
        self.assertTrue(dest.exists())
        self.assertEqual(report.output, dest)
        self.assertGreater(wav_duration(dest), 0.5)

    def test_work_dir_is_cleaned_up(self):
        report = speak(LONG_TEXT, self.config(), bridge=self.bridge)
        self.assertTrue(report.results)
        leftovers = list((self.tmp / "work" / "run").glob("*/*.wav"))
        self.assertEqual(leftovers, [])

    def test_synth_to_file_leaves_no_intermediate_wav(self):
        dest = self.tmp / "out" / "speech.wav"
        synth_to_file(LONG_TEXT, self.config(), dest, bridge=self.bridge)
        self.assertTrue(dest.exists())
        leftovers = list((self.tmp / "work" / "run").glob("*/*.wav"))
        self.assertEqual(leftovers, [], "ブロックごとの中間 wav が残っている")

    def test_stale_work_dir_is_removed_on_next_run(self):
        # on_busy=replace で SIGKILL された読み上げの残骸を模す
        stale = self.tmp / "work" / "run" / "999999-stale"
        stale.mkdir(parents=True)
        (stale / "0001.wav").write_bytes(b"x" * 64)
        speak(LONG_TEXT, self.config(), bridge=self.bridge)
        self.assertFalse(stale.exists())

    def test_prepare_blocks_matches_synth_calls(self):
        cfg = self.config()
        blocks = prepare_blocks(LONG_TEXT, cfg)
        speak(LONG_TEXT, cfg, bridge=self.bridge)
        calls = self.recorded_calls()
        self.assertEqual(len(blocks), len(calls))


if __name__ == "__main__":
    unittest.main()
