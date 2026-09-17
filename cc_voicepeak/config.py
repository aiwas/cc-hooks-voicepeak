"""設定の読み込みとデフォルト値.

優先順位 (後のものが前のものを上書き):

1. 組み込みデフォルト
2. ``~/.config/cc-voicepeak/config.json`` (XDG_CONFIG_HOME 対応)
3. プロジェクト内 ``.claude/voicepeak.json``
   (``CLAUDE_PROJECT_DIR`` があればそこ、無ければ cwd)
4. 環境変数 ``CC_VOICEPEAK_CONFIG`` が指すファイル
5. 環境変数による個別上書き (``CC_VOICEPEAK_*``)
6. ``--config`` で指定したファイル
7. コマンドライン引数

4 までがファイル、5 以降がその場の指定。``--config`` はコマンドライン引数なので
環境変数より後に重ねる。
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .normalize import DEFAULT_OPTIONS as _NORMALIZE_DEFAULTS

# voicepeak が 1 回の起動で受け付ける最大文字数。
# 141 文字以上を渡すとエラーになり wav が出力されないため、既定は安全側の 140。
VOICEPEAK_CHAR_LIMIT = 140

DEFAULTS: dict[str, Any] = {
    # ---- voicepeak 本体 -------------------------------------------------
    "voicepeak": {
        # WSL パス (/mnt/c/...) でも Windows パス (C:\...) でも可。null なら自動探索。
        "exe": None,
        "narrator": None,          # 例: "Miyamai Moca" / "女性1"
        "emotion": None,           # 例: "happy=50,sad=0"
        "speed": None,             # 50-200
        "pitch": None,             # -300-300
        "char_limit": VOICEPEAK_CHAR_LIMIT,
        # say: -s で argv 渡し / text_file: -t でファイル渡し
        "input_mode": "say",
        # 1 ブロックあたりのタイムアウト秒
        "timeout": 120,
        # 失敗時のリトライ回数 (input_mode を切り替えて再試行する)
        "retries": 1,
        # 同じ文面 + 同じ声の wav を再利用する
        "cache": True,
        "cache_max_files": 400,
    },
    # ---- 分割 -----------------------------------------------------------
    "split": {
        # 文字数の数え方: "codepoints" (voicepeak と同じ) / "halfwidth_half"
        "width_mode": "codepoints",
        # ブロックをどれくらい詰めてから区切るか (0.0-1.0)。
        # 大きいほど EXE 起動回数が減り、小さいほど区切りが自然になる。
        "min_fill": 0.55,
        # 最後のブロックが極端に短い場合に前のブロックと均す
        "balance_tail": True,
        # 空白のみ/記号のみのブロックを捨てる
        "drop_empty": True,
    },
    # ---- 読み上げ用テキスト整形 ------------------------------------------
    # 各キーの既定値と説明は normalize.DEFAULT_OPTIONS を正本とする。
    # `replacements` がリストなので deepcopy で切り離す (共有すると片方への
    # 変更がもう片方に漏れる)。`enabled` だけは normalize() が見ない設定側のキー。
    "normalize": {"enabled": True, **copy.deepcopy(_NORMALIZE_DEFAULTS)},
    # ---- 再生 -----------------------------------------------------------
    "player": {
        # "auto" / "powershell" / "paplay" / "aplay" / "ffplay" / "none"
        "backend": "auto",
        # true: 全ブロックを 1 本の wav に連結してから再生 (完全に無音間隔なし)
        # false: 合成できたブロックから順次再生 (初音までが速い)
        "concat": False,
        "volume": None,            # ffplay/paplay 用 (0-100)
    },
    # ---- Hook 動作 ------------------------------------------------------
    "hook": {
        # 反応するイベント
        "events": ["Stop", "Notification"],
        # 同じセッションで前の読み上げが残っている場合の挙動:
        # "replace" (割り込み) / "queue" (待つ) / "skip" (捨てる)
        "on_busy": "replace",
        # これより短いテキストは読まない
        "min_chars": 2,
        # 読み上げの前後に付ける定型句
        "prefix": "",
        "suffix": "",
        # Notification イベント用の定型句
        "notification_prefix": "",
        # SubagentStop で読み上げるか
        "subagent": False,
    },
    "log": {
        # null なら $XDG_STATE_HOME/cc-voicepeak/cc-voicepeak.log
        "file": None,
        "level": "info",           # debug / info / warning / error / off
        "max_bytes": 1048576,
    },
}

_ENV_MAP = {
    "CC_VOICEPEAK_EXE": ("voicepeak", "exe"),
    "CC_VOICEPEAK_NARRATOR": ("voicepeak", "narrator"),
    "CC_VOICEPEAK_EMOTION": ("voicepeak", "emotion"),
    "CC_VOICEPEAK_SPEED": ("voicepeak", "speed"),
    "CC_VOICEPEAK_PITCH": ("voicepeak", "pitch"),
    "CC_VOICEPEAK_CHAR_LIMIT": ("voicepeak", "char_limit"),
    "CC_VOICEPEAK_INPUT_MODE": ("voicepeak", "input_mode"),
    "CC_VOICEPEAK_PLAYER": ("player", "backend"),
    "CC_VOICEPEAK_LOG_LEVEL": ("log", "level"),
    "CC_VOICEPEAK_LOG_FILE": ("log", "file"),
}

_INT_KEYS = {
    ("voicepeak", "speed"),
    ("voicepeak", "pitch"),
    ("voicepeak", "char_limit"),
    ("voicepeak", "timeout"),
    ("voicepeak", "retries"),
    ("voicepeak", "cache_max_files"),
    ("normalize", "max_total_chars"),
    ("hook", "min_chars"),
    ("log", "max_bytes"),
}


class Config:
    """ネストした dict を ``cfg.get("voicepeak.narrator")`` で引ける薄いラッパ."""

    def __init__(self, data: dict[str, Any], sources: list[Path] | None = None):
        self._data = data
        self.sources: list[Path] = sources or []

    # -- アクセサ ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"Config(sources={[str(p) for p in self.sources]})"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _coerce(path: tuple, value: Any) -> Any:
    if path in _INT_KEYS and isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{'.'.join(path)} は整数で指定してください: {value!r}") from exc
    return value


def _coerce_known_keys(data: dict[str, Any]) -> None:
    """設定ファイル由来の値にも型変換をかける.

    環境変数と CLI 引数は ``_coerce()`` を通っていたが、JSON 由来の値は
    素通しだったため ``{"voicepeak": {"speed": "fast"}}`` が生の ValueError に
    なっていた。ここで ``ConfigError`` に揃える。
    """
    for path in _INT_KEYS:
        node: Any = data
        for part in path[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and path[-1] in node:
            node[path[-1]] = _coerce(path, node[path[-1]])


# 以前はここも暗黙に読んでいた。移行の案内のために場所だけ残す (下記 legacy_config_path)。
LEGACY_PROJECT_CONFIG = Path(".claude") / "voicepeak.json"


def config_search_paths() -> list[Path]:
    """探索対象の設定ファイルを優先度の低い順に返す.

    **プロジェクト側のファイルは探索しない。** 読み上げの設定は
    ``voicepeak.exe`` のパスや持っている声など、そのホスト固有の値が中心で、
    clone したリポジトリが知り得る内容ではない。一方で
    ``$CLAUDE_PROJECT_DIR/.claude/voicepeak.json`` を暗黙に読むと、
    リポジトリを開いただけで ``voicepeak.exe`` (任意の実行ファイル) や
    ``log.file`` (任意パスへの追記と rename) が持ち込まれる。Claude Code の
    フォルダ信頼ダイアログは ``settings.json`` が対象でこのファイルを含まないため、
    「自分で書いた設定」と「clone に付いてきた設定」を実行時に区別できない。
    プロジェクト単位で変えたい場合は ``CC_VOICEPEAK_CONFIG`` か ``--config`` で
    明示する。
    """
    paths: list[Path] = []

    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg) if xdg else Path.home() / ".config"
    paths.append(config_home / "cc-voicepeak" / "config.json")

    explicit = os.environ.get("CC_VOICEPEAK_CONFIG")
    if explicit:
        paths.append(Path(explicit).expanduser())

    return paths


def legacy_config_path() -> Path | None:
    """読まなくなったプロジェクト設定が置かれていればそのパスを返す.

    黙って無視すると「設定が効かなくなった」としか見えないので、
    ``check`` が移行を案内するために使う。
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    roots = [Path(project)] if project else [Path.cwd()]
    for root in roots:
        path = root / LEGACY_PROJECT_CONFIG
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def unknown_keys(cfg: Config) -> list[str]:
    """``DEFAULTS`` に無いキーを ``"voicepeak.narator"`` の形で列挙する.

    綴り誤りは黙って無視されてしまうので、``check`` で警告するために使う。
    """
    return sorted(_walk_unknown(cfg.as_dict(), DEFAULTS, ""))


def _walk_unknown(data: dict[str, Any], defaults: dict[str, Any], prefix: str) -> list[str]:
    found: list[str] = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in defaults:
            found.append(dotted)
            continue
        if isinstance(value, dict) and isinstance(defaults[key], dict):
            found.extend(_walk_unknown(value, defaults[key], f"{dotted}."))
    return found


def _merge_files(data: dict[str, Any], paths: list[Path], used: list[Path]) -> None:
    """設定ファイルを順に読み込んで ``data`` へ重ねる."""
    for path in paths:
        try:
            if not path.is_file():
                continue
            with path.open(encoding="utf-8") as handle:
                loaded = json.load(handle)
        except OSError:
            continue
        except json.JSONDecodeError as exc:
            raise ConfigError(f"設定ファイルの JSON が壊れています: {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"設定ファイルのトップレベルはオブジェクトにしてください: {path}")
        _deep_merge(data, loaded)
        used.append(path)


def load_config(
    extra_paths: list[Path] | None = None,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
) -> Config:
    """設定を読み込んで :class:`Config` を返す."""
    data = copy.deepcopy(DEFAULTS)
    used: list[Path] = []

    _merge_files(data, config_search_paths(), used)

    if use_env:
        for env_name, path_tuple in _ENV_MAP.items():
            raw = os.environ.get(env_name)
            if raw is None or raw == "":
                continue
            node = data
            for part in path_tuple[:-1]:
                node = node.setdefault(part, {})
            node[path_tuple[-1]] = _coerce(path_tuple, raw)

    # --config はコマンドライン引数なので、環境変数より後に重ねる
    if extra_paths:
        _merge_files(data, [Path(p).expanduser() for p in extra_paths], used)

    _coerce_known_keys(data)
    cfg = Config(data, used)

    for dotted, value in (overrides or {}).items():
        if value is None:
            continue
        cfg.set(dotted, _coerce(tuple(dotted.split(".")), value))

    _validate(cfg)
    return cfg


def _check_int(cfg: Config, key: str, low: int, high: int | None = None) -> None:
    """``None`` を許す整数キーの範囲検査 (bool は整数として扱わない)."""
    value = cfg.get(key)
    if value is None:
        return
    limit = f"{low} 以上" if high is None else f"{low}-{high}"
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} は整数で指定してください ({limit}): {value!r}")
    if value < low or (high is not None and value > high):
        raise ConfigError(f"{key} は {limit} です: {value!r}")


def _check_choice(cfg: Config, key: str, choices: Sequence[str]) -> None:
    value = cfg.get(key)
    if value not in choices:
        allowed = " / ".join(repr(choice) for choice in choices)
        raise ConfigError(f"{key} は {allowed} のいずれかです: {value!r}")


def _validate(cfg: Config) -> None:
    limit = cfg.get("voicepeak.char_limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= VOICEPEAK_CHAR_LIMIT:
        raise ConfigError(
            f"voicepeak.char_limit は 1..{VOICEPEAK_CHAR_LIMIT} で指定してください: {limit!r}"
        )

    min_fill = cfg.get("split.min_fill")
    if (
        isinstance(min_fill, bool)
        or not isinstance(min_fill, (int, float))
        or not 0.0 <= float(min_fill) <= 1.0
    ):
        raise ConfigError(f"split.min_fill は 0.0-1.0 の数値です: {min_fill!r}")

    _check_int(cfg, "voicepeak.speed", 50, 200)
    _check_int(cfg, "voicepeak.pitch", -300, 300)
    _check_int(cfg, "voicepeak.timeout", 1)
    _check_int(cfg, "voicepeak.retries", 0)
    _check_int(cfg, "voicepeak.cache_max_files", 0)
    _check_int(cfg, "normalize.max_total_chars", 0)
    _check_int(cfg, "hook.min_chars", 0)
    _check_int(cfg, "player.volume", 0, 100)
    _check_int(cfg, "log.max_bytes", 1)

    _check_choice(cfg, "voicepeak.input_mode", ("say", "text_file"))
    _check_choice(cfg, "split.width_mode", ("codepoints", "halfwidth_half"))
    _check_choice(cfg, "normalize.code_blocks", ("drop", "placeholder", "read"))
    _check_choice(cfg, "normalize.inline_code", ("read", "drop"))
    _check_choice(cfg, "normalize.tables", ("drop", "read"))
    _check_choice(
        cfg, "player.backend", ("auto", "powershell", "paplay", "aplay", "ffplay", "none")
    )
    _check_choice(cfg, "hook.on_busy", ("replace", "queue", "skip"))
    _check_choice(cfg, "log.level", ("debug", "info", "warning", "error", "off"))

    events = cfg.get("hook.events")
    if not isinstance(events, list) or not all(isinstance(event, str) for event in events):
        raise ConfigError(f"hook.events は文字列のリストです: {events!r}")
