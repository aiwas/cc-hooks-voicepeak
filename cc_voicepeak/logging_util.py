"""ログ設定. Hook から呼ばれるためログはファイルに出す (stdout は汚さない)."""

from __future__ import annotations

import fcntl
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .fsutil import xdg_state_home

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


class MultiProcessRotatingFileHandler(RotatingFileHandler):
    """プロセス間ロック付きの :class:`RotatingFileHandler`.

    hook プロセスとデタッチされた読み上げプロセスが同じログファイルへ同時に
    書くため、素の ``RotatingFileHandler`` ではローテーションが重なって
    行が混ざったり失われたりする。``flock`` で 1 レコードずつ直列化し、
    他プロセスがローテーションしていたら開き直す。
    """

    def __init__(
        self,
        filename,
        maxBytes: int = 0,
        backupCount: int = 0,
        encoding: str | None = None,
    ):
        # ロックを取ってから開きたいので遅延オープンにする
        super().__init__(
            filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=True,
        )
        self._lock_path = str(self.baseFilename) + ".lock"
        self._guard = None

    def _acquire_guard(self):
        if self._guard is None:
            self._guard = open(self._lock_path, "a+")  # noqa: SIM115 - close() で閉じる
        fcntl.flock(self._guard, fcntl.LOCK_EX)
        return self._guard

    def _reopen_if_rotated(self) -> None:
        """他プロセスがローテーションしていたら掴み直す.

        開いたままだと、退避されたファイルへ書き続けてしまう。
        """
        if self.stream is None:
            return
        try:
            current = os.fstat(self.stream.fileno()).st_ino
            latest = os.stat(self.baseFilename).st_ino
        except OSError:
            current, latest = 0, 1
        if current != latest:
            self.stream.close()
            self.stream = None  # 次の emit で開き直される

    def emit(self, record: logging.LogRecord) -> None:
        try:
            guard = self._acquire_guard()
        except OSError:
            # ロックを取れなくてもログ自体は落とさない
            super().emit(record)
            return
        try:
            self._reopen_if_rotated()
            super().emit(record)
        finally:
            try:
                fcntl.flock(guard, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - 解放に失敗しても続行する
                pass

    def close(self) -> None:
        try:
            if self._guard is not None:
                self._guard.close()
                self._guard = None
        finally:
            super().close()


def default_log_path() -> Path:
    return xdg_state_home() / "cc-voicepeak" / "cc-voicepeak.log"


def setup_logging(
    level: str = "info",
    file: str | None = None,
    max_bytes: int = 1048576,
    stderr: bool = False,
) -> logging.Logger:
    """ルートロガーを設定して ``cc_voicepeak`` ロガーを返す."""
    global _configured
    logger = logging.getLogger("cc_voicepeak")
    if _configured:
        return logger

    resolved = _LEVELS.get(str(level).lower(), logging.INFO)
    logger.setLevel(resolved)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s"
    )

    # "off" のときは 1 行も出ないので、ファイルもディレクトリも作らない
    if resolved <= logging.CRITICAL:
        path = Path(file).expanduser() if file else default_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = MultiProcessRotatingFileHandler(
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
