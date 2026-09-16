"""ユーザ専用ディレクトリの用意.

作業ディレクトリやランタイムディレクトリを ``/tmp`` のような全ユーザ共有の
場所へ置くと、他ユーザが先回りして同名のディレクトリを作れる。
その中に置かれた ``cache/<sha1>.wav`` や状態ファイルは中身を検証せずに
使われるため、置き場所そのものが自分専用であることを要求する。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# 所有者以外にビットが立っていたら「自分専用」とは言えない
_OTHERS_MASK = 0o077


def xdg_state_home() -> Path:
    """``$XDG_STATE_HOME`` (既定 ``~/.local/state``)."""
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) if base else Path.home() / ".local" / "state"


def ensure_private_dir(path: Path) -> Path:
    """``path`` を自分専用のディレクトリとして用意し、そのパスを返す.

    無ければ ``0700`` で作る。既にある場合は次の 3 つを検査し、
    満たさなければ :class:`PermissionError` を投げる。

    * シンボリックリンクではない
      (``mkdir(exist_ok=True)`` はリンク先がディレクトリなら黙って通す)
    * 所有者が自分である
    * 所有者以外にアクセス権が無い (自分の所有なら ``0700`` へ絞り直す)

    権限の絞り直しに失敗した場合も :class:`PermissionError` にする。
    黙って続行すると、他ユーザが書ける場所をそのまま使ってしまう。
    """
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise PermissionError(f"{path} を用意できません: {exc}") from exc

    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"{path} はシンボリックリンクです")
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"{path} はディレクトリではありません")
    if info.st_uid != os.getuid():
        raise PermissionError(
            f"{path} の所有者が自分 (uid={os.getuid()}) ではありません (uid={info.st_uid})"
        )
    if stat.S_IMODE(info.st_mode) & _OTHERS_MASK:
        # 自分の所有で、かつシンボリックリンクでないことは確認済みなので
        # chmod がリンク先へ波及することはない
        try:
            path.chmod(0o700)
            info = path.lstat()
        except OSError as exc:
            raise PermissionError(f"{path} の権限を 0700 にできません: {exc}") from exc
        if stat.S_IMODE(info.st_mode) & _OTHERS_MASK:
            raise PermissionError(
                f"{path} の権限を絞れません (mode={stat.S_IMODE(info.st_mode):04o})"
            )
    return path
