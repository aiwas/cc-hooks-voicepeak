"""バージョン番号が 1 か所に集約され、手書き箇所が一致していることの検査。

`pyproject.toml` は `cc_voicepeak.__version__` を動的に参照するので手書きしない。
Claude Code が読む `plugin.json` / `marketplace.json` は JSON のため参照で済ませられず、
手書きが残る。この 2 か所を `__version__` と突き合わせる。
"""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path

from cc_voicepeak import __version__

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / ".claude-plugin"


class VersionConsistencyTest(unittest.TestCase):
    def test_version_looks_like_a_release(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_plugin_manifest_matches_package(self):
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], __version__)

    def test_marketplace_entries_match_package(self):
        marketplace = json.loads(
            (PLUGIN_DIR / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertTrue(marketplace["plugins"])
        for entry in marketplace["plugins"]:
            with self.subTest(plugin=entry["name"]):
                self.assertEqual(entry["version"], __version__)

    def test_pyproject_does_not_hardcode_version(self):
        with (REPO / "pyproject.toml").open("rb") as fh:
            pyproject = tomllib.load(fh)
        self.assertNotIn("version", pyproject["project"])
        self.assertIn("version", pyproject["project"]["dynamic"])
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "cc_voicepeak.__version__"},
        )


if __name__ == "__main__":
    unittest.main()
