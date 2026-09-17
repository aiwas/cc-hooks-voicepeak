"""設定読み込みのテスト."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from cc_voicepeak.config import (
    VOICEPEAK_CHAR_LIMIT,
    config_search_paths,
    legacy_config_path,
    load_config,
    unknown_keys,
)
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

    def test_project_config_is_not_searched(self):
        """clone に付いてきた設定を暗黙に読まない (信頼スコープの分離)."""
        self.write(
            Path(os.environ["XDG_CONFIG_HOME"]) / "cc-voicepeak" / "config.json",
            {"voicepeak": {"narrator": "ユーザー"}},
        )
        self.write(
            Path(os.environ["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json",
            {"voicepeak": {"narrator": "プロジェクト"}},
        )
        self.assertEqual(load_config().get("voicepeak.narrator"), "ユーザー")

    def test_project_config_is_not_in_the_search_paths(self):
        for path in config_search_paths():
            self.assertNotIn(".claude", path.parts)

    def test_legacy_project_config_is_reported(self):
        self.assertIsNone(legacy_config_path())
        path = self.write(
            Path(os.environ["CLAUDE_PROJECT_DIR"]) / ".claude" / "voicepeak.json",
            {"voicepeak": {"narrator": "プロジェクト"}},
        )
        self.assertEqual(legacy_config_path(), path)

    def test_env_overrides_files(self):
        self.write(
            Path(os.environ["XDG_CONFIG_HOME"]) / "cc-voicepeak" / "config.json",
            {"voicepeak": {"narrator": "ファイル"}},
        )
        os.environ["CC_VOICEPEAK_NARRATOR"] = "環境変数"
        self.assertEqual(load_config().get("voicepeak.narrator"), "環境変数")

    def test_explicit_config_beats_env(self):
        # --config はコマンドライン引数なので環境変数より優先する
        extra = self.write(self.tmp / "extra.json", {"voicepeak": {"narrator": "--config"}})
        os.environ["CC_VOICEPEAK_NARRATOR"] = "環境変数"
        cfg = load_config(extra_paths=[extra])
        self.assertEqual(cfg.get("voicepeak.narrator"), "--config")

    def test_env_config_file_loses_to_env_variable(self):
        # CC_VOICEPEAK_CONFIG はファイルなので環境変数の個別指定に負ける
        path = self.write(self.tmp / "env.json", {"voicepeak": {"narrator": "ファイル"}})
        os.environ["CC_VOICEPEAK_CONFIG"] = str(path)
        os.environ["CC_VOICEPEAK_NARRATOR"] = "環境変数"
        self.assertEqual(load_config().get("voicepeak.narrator"), "環境変数")

    def in_cwd(self, data: dict) -> Path:
        """cwd を一時ディレクトリへ移し、そこに .claude/voicepeak.json を置く."""
        cwd = self.tmp / "work"
        path = self.write(cwd / ".claude" / "voicepeak.json", data)
        previous = Path.cwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, previous)
        return path

    def test_cwd_is_not_searched(self):
        """任意のディレクトリで実行しただけで設定を拾わない."""
        del os.environ["CLAUDE_PROJECT_DIR"]
        self.in_cwd({"voicepeak": {"narrator": "cwd"}})
        self.assertIsNone(load_config().get("voicepeak.narrator"))

    def test_legacy_cwd_config_is_reported_without_project_dir(self):
        del os.environ["CLAUDE_PROJECT_DIR"]
        path = self.in_cwd({"voicepeak": {"narrator": "cwd"}})
        self.assertEqual(legacy_config_path(), path)

    def test_cli_overrides_win(self):
        os.environ["CC_VOICEPEAK_NARRATOR"] = "環境変数"
        cfg = load_config(overrides={"voicepeak.narrator": "コマンドライン"})
        self.assertEqual(cfg.get("voicepeak.narrator"), "コマンドライン")

    def test_env_numeric_is_coerced(self):
        os.environ["CC_VOICEPEAK_CHAR_LIMIT"] = "100"
        self.assertEqual(load_config().get("voicepeak.char_limit"), 100)

    def user_config_path(self) -> Path:
        return Path(os.environ["XDG_CONFIG_HOME"]) / "cc-voicepeak" / "config.json"

    def user_config(self, data: dict) -> Path:
        return self.write(self.user_config_path(), data)

    def test_file_numeric_string_is_coerced(self):
        self.user_config({"voicepeak": {"speed": "120"}})
        self.assertEqual(load_config().get("voicepeak.speed"), 120)

    def test_file_non_numeric_string_raises_config_error(self):
        self.user_config({"voicepeak": {"speed": "fast"}})
        with self.assertRaises(ConfigError):
            load_config()

    def test_file_min_chars_string_raises_config_error(self):
        self.user_config({"hook": {"min_chars": "x"}})
        with self.assertRaises(ConfigError):
            load_config()

    def test_broken_json_raises(self):
        path = self.user_config_path()
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

    def test_out_of_range_values_are_rejected(self):
        cases = [
            {"voicepeak": {"pitch": 9999}},
            {"voicepeak": {"timeout": 0}},
            {"voicepeak": {"retries": -1}},
            {"voicepeak": {"cache_max_files": -1}},
            {"normalize": {"max_total_chars": -1}},
            {"hook": {"min_chars": -1}},
            {"player": {"volume": 200}},
            {"split": {"min_fill": 1.5}},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.user_config(data)
                with self.assertRaises(ConfigError):
                    load_config()

    def test_invalid_enum_values_are_rejected(self):
        cases = [
            {"voicepeak": {"input_mode": "stdin"}},
            {"split": {"width_mode": "???"}},
            {"normalize": {"code_blocks": "???"}},
            {"normalize": {"inline_code": "???"}},
            {"normalize": {"tables": "???"}},
            {"hook": {"on_busy": "wait"}},
            {"log": {"level": "trace"}},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.user_config(data)
                with self.assertRaises(ConfigError):
                    load_config()

    def test_hook_events_must_be_a_list_of_strings(self):
        self.user_config({"hook": {"events": "Stop"}})
        with self.assertRaises(ConfigError):
            load_config()

    def test_boolean_is_not_accepted_as_integer(self):
        self.user_config({"hook": {"min_chars": True}})
        with self.assertRaises(ConfigError):
            load_config()

    def test_top_level_must_be_an_object(self):
        path = self.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config()

    def test_unknown_keys_are_listed(self):
        self.user_config({"voicepeak": {"narator": "誤字"}, "unknown_section": {}})
        self.assertEqual(
            unknown_keys(load_config()), ["unknown_section", "voicepeak.narator"]
        )

    def test_no_unknown_keys_by_default(self):
        self.assertEqual(unknown_keys(load_config()), [])

    def test_sources_are_reported(self):
        path = self.write(
            Path(os.environ["XDG_CONFIG_HOME"]) / "cc-voicepeak" / "config.json", {}
        )
        self.assertIn(path, load_config().sources)
