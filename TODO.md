# TODO / 改善候補

コードレビューで洗い出した項目。優先度順。
「[確認済]」は手元で実際に再現・実測したもの、それ以外はコードを読んで指摘したもの。
洗い出した時点でテストは 134 件すべて成功していたため、以下はいずれも当時の
テストでは検出されない問題である。

対応済みの項目は本ファイルから削除する（履歴は `git log` を参照）。

---

## 優先度: 低

- **`synth.py:129`** 一時領域が `/mnt/c/Windows/Temp` になった場合、`cache/<sha1>.wav` が
  予測可能な名前で他ユーザから書き換え可能な場所に置かれる
- **`pipeline.py:139`** 空入力時に `report.elapsed` が設定されず `summary()` が不正確
- **`pyproject.toml`** `[project.urls]` と `Operating System ::` /
  `Programming Language :: Python :: 3.9` 系の細目 classifier が無い。
  `license = { file = "LICENSE" }` と `License ::` classifier の併用は
  setuptools>=77 で非推奨（`license = "MIT"` + `license-files` へ）
- **`examples/settings.json:2`** トップレベルの `"$comment"` は Claude Code の settings
  スキーマが警告する可能性がある（未検証）。コピー元として配布するファイルなので
  コメントは README 側に置く方が安全

---

## テストの穴

以下は未検証（2026-09-15 時点、テストは 345 件）。

- **CLI** — `speak --player`、`speak --concat`


---

## 実機確認が必要な項目

- **codepoints 換算と VOICEPEAK の実カウント** — `width_mode="codepoints"` は
  VOICEPEAK が Unicode コードポイント数で 140 を判定する前提。内部が UTF-16
  コードユニット数で数えている場合、星域面の文字が 2 とカウントされ、
  140 文字ちょうどのブロックが拒否される可能性がある
- **`hook.py:64` の `isSidechain`** — `include_sidechain=bool(payload.get("isSidechain"))`
  としているが、`SubagentStop` の payload に `isSidechain` が含まれない場合、
  サブエージェント自身の最終応答がスキップされ、メインエージェントの古いテキストが
  読み上げられる。実際の payload 形状の確認が必要
- **Python 3.9 での動作** — `pyproject.toml` は 3.9+ を宣言しているが、
  手元にあるのは 3.13 のみ。3.9 系での実行は未検証
