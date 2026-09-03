"""ログ設定. Hook から呼ばれるためログはファイルに出す (stdout は汚さない)."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "off": logging.CRITICAL + 10,
}

_configured = False

# setup_logging を呼ばない経路 (ライブラリ利用/テスト) で
# lastResort ハンドラが stderr を汚さないようにする
logging.getLogger("cc_voicepeak").addHandler(logging.NullHandler())


def default_log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cc-voicepeak" / "cc-voicepeak.log"


def setup_logging(
    level: str = "info",
    file: Optional[str] = None,
    max_bytes: int = 1048576,
    stderr: bool = False,
) -> logging.Logger:
    """ルートロガーを設定して ``cc_voicepeak`` ロガーを返す."""
    global _configured
    logger = logging.getLogger("cc_voicepeak")
    if _configured:
        return logger

    logger.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s"
    )

    path = Path(file).expanduser() if file else default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=max(max_bytes, 4096), backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError:
        pass

    if stderr:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(stream)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    _configured = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger("cc_voicepeak" + (f".{name}" if name else ""))
