"""設定読み込みのテスト."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from cc_voicepeak.config import VOICEPEAK_CHAR_LIMIT, load_config
from cc_voicepeak.errors import ConfigError


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-cfg-"))
        self._env = dict(os.environ)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.tmp / "project")
        for name in list(os.environ):
            if name.startswith("CC_VOICEPEAK_"):
                del os.environ[name]

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path: Path, data: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_defaults(self):
        cfg = load_config()
        self.assertEqual(cfg.get("voicepeak.char_limit"), VOICEPEAK_CHAR_LIMIT)
        self.assertEqual(cfg.get("player.backend"), "auto")

    def test_user_config_is_merged(self):
        self.write(
            Path(os.environ["XDG_CONFIG_HOME"]) / "cc-voicepeak" / "config.json",
            {"voicepeak": {"narrator": "ユーザー設定"}},
        )
        cfg = load_config()
        self.assertEqual(cfg.get("voicepeak.narrator"), "ユーザー設定")
        # 指定していないキーは既定値のまま
        self.assertEqual(cfg.get("voicepeak.char_limit"), VOICEPEAK_CHAR_LIMIT)

    def test_project_config_wins_over_user_config(self):
        self.write(
            Path(os.environ["XDG_CONFIG_HOME"]) / "cc-voicepeak" / "config.json",
            {"voicepeak": {"narrator": "ユーザー"}},
        )
        self.write(
            Path(os.environ["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json",
            {"voicepeak": {"narrator": "プロジェクト"}},
        )
        self.assertEqual(load_config().get("voicepeak.narrator"), "プロジェクト")

    def test_env_overrides_files(self):
        self.write(
            Path(os.environ["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json",
            {"voicepeak": {"narrator": "ファイル"}},
        )
        os.environ["CC_VOICEPEAK_NARRATOR"] = "環境変数"
        self.assertEqual(load_config().get("voicepeak.narrator"), "環境変数")

    def test_cli_overrides_win(self):
        os.environ["CC_VOICEPEAK_NARRATOR"] = "環境変数"
        cfg = load_config(overrides={"voicepeak.narrator": "コマンドライン"})
        self.assertEqual(cfg.get("voicepeak.narrator"), "コマンドライン")

    def test_env_numeric_is_coerced(self):
        os.environ["CC_VOICEPEAK_CHAR_LIMIT"] = "100"
        self.assertEqual(load_config().get("voicepeak.char_limit"), 100)

    def project_config(self, data: dict) -> Path:
        return self.write(
            Path(os.environ["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json", data
        )

    def test_file_numeric_string_is_coerced(self):
        self.project_config({"voicepeak": {"speed": "120"}})
        self.assertEqual(load_config().get("voicepeak.speed"), 120)

    def test_file_non_numeric_string_raises_config_error(self):
        self.project_config({"voicepeak": {"speed": "fast"}})
        with self.assertRaises(ConfigError):
            load_config()

    def test_file_min_chars_string_raises_config_error(self):
        self.project_config({"hook": {"min_chars": "x"}})
        with self.assertRaises(ConfigError):
            load_config()

    def test_broken_json_raises(self):
        path = Path(os.environ["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config()

    def test_char_limit_above_140_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(overrides={"voicepeak.char_limit": 200})

    def test_invalid_speed_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(overrides={"voicepeak.speed": 999})

    def test_invalid_player_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(overrides={"player.backend": "mplayer"})

    def test_sources_are_reported(self):
        path = self.write(
            Path(os.environ["XDG_CONFIG_HOME"]) / "cc-voicepeak" / "config.json", {}
        )
        self.assertIn(path, load_config().sources)
