# TODO / 改善候補

コードレビューで洗い出した項目。優先度順。
「[確認済]」は手元で実際に再現・実測したもの、それ以外はコードを読んで指摘したもの。

対応済みの項目は本ファイルから削除する（履歴は `git log` を参照）。

最終棚卸: 2026-09-16（テスト 346 件すべて成功）。
中〜大の指摘はすべて解消済みで、残りは以下の低優先度・未検証項目のみ。

---

## 優先度: 低

- **`synth.py:211`** 一時領域が `/mnt/c/Windows/Temp` になった場合、`cache/<sha1>.wav` が
  予測可能な名前で他ユーザから書き換え可能な場所に置かれる。
  `bridge._windows_temp_candidates()` の**最後の候補**なので、`%TEMP%` の取得と
  `C:/Users/<USER>/AppData/Local/Temp` の両方が外れたときだけ到達する

---

## テストの穴

以下は未検証（2026-09-16 時点、テストは 346 件）。

- **CLI** — `speak --player`、`speak --concat`。
  どちらも `cli.py:77-79` で受け取っているが、`tests/test_cli.py` から渡すケースが無い

---

## 実機確認が必要な項目

- **codepoints 換算と VOICEPEAK の実カウント** — `width_mode="codepoints"` は
  VOICEPEAK が Unicode コードポイント数で 140 を判定する前提。内部が UTF-16
  コードユニット数で数えている場合、星域面の文字が 2 とカウントされ、
  140 文字ちょうどのブロックが拒否される可能性がある
- **`hook.py:73` の `isSidechain`** — `include_sidechain=bool(payload.get("isSidechain"))`
  としているが、`SubagentStop` の payload に `isSidechain` が含まれない場合、
  サブエージェント自身の最終応答がスキップされ、メインエージェントの古いテキストが
  読み上げられる。実際の payload 形状の確認が必要
- **Python 3.9 での動作** — 静的には 3.9 互換を確認済み（全モジュールが
  `ast.parse(feature_version=(3,9))` を通り、3.10 以降で追加された標準ライブラリ API の
  使用も無い。組み込みジェネリクス `list[str]` 等は未使用で、実行時に注釈を評価する
  `get_type_hints()` の呼び出しも無い）。ただし手元にあるのは 3.13 のみで、
  3.9 系インタプリタでのテスト実行は未検証
