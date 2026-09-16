"""ユーザ専用ディレクトリの検査のテスト.

``ensure_private_dir()`` は作業ディレクトリ (bridge) の最後の手段で使う。
他ユーザが先回りして作ったディレクトリを黙って使わないことを確認する。
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cc_voicepeak.fsutil import ensure_private_dir


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class EnsurePrivateDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = self.tmp / "nested" / "work"

    def test_creates_with_0700(self):
        # umask が緩くても 0700 で作る
        old = os.umask(0o000)
        self.addCleanup(os.umask, old)
        result = ensure_private_dir(self.target)
        self.assertEqual(result, self.target)
        self.assertTrue(self.target.is_dir())
        self.assertEqual(_mode(self.target), 0o700)

    def test_existing_private_dir_is_accepted(self):
        self.target.mkdir(parents=True, mode=0o700)
        self.assertEqual(ensure_private_dir(self.target), self.target)

    def test_tightens_permissions_of_own_dir(self):
        """自分の所有なら 0755 のような緩い権限を 0700 へ絞る."""
        self.target.mkdir(parents=True)
        self.target.chmod(0o755)
        ensure_private_dir(self.target)
        self.assertEqual(_mode(self.target), 0o700)

    def test_rejects_symlink(self):
        """mkdir(exist_ok=True) はリンク先がディレクトリなら通すため、lstat で弾く."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(elsewhere)
        with self.assertRaises(PermissionError) as ctx:
            ensure_private_dir(self.target)
        self.assertIn("シンボリックリンク", str(ctx.exception))
        # リンク先の権限に触れていない
        self.assertEqual(_mode(elsewhere), 0o700)

    def test_rejects_regular_file(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("x", encoding="utf-8")
        with self.assertRaises(PermissionError):
            ensure_private_dir(self.target)

    def test_rejects_foreign_owner(self):
        """他ユーザが所有するディレクトリは、権限が絞られていても使わない."""
        self.target.mkdir(parents=True, mode=0o700)
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError) as ctx:
                ensure_private_dir(self.target)
        self.assertIn("所有者", str(ctx.exception))

    def test_rejects_when_permissions_cannot_be_tightened(self):
        """chmod に失敗したら続行しない (緩いまま使うことになるため)."""
        self.target.mkdir(parents=True)
        self.target.chmod(0o777)
        with mock.patch.object(Path, "chmod", side_effect=PermissionError("EPERM")):
            with self.assertRaises(PermissionError) as ctx:
                ensure_private_dir(self.target)
        self.assertIn("0700", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
