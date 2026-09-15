# TODO / 改善候補

コードレビューで洗い出した項目。優先度順。
「[確認済]」は手元で実際に再現・実測したもの、それ以外はコードを読んで指摘したもの。
洗い出した時点でテストは 134 件すべて成功していたため、以下はいずれも当時の
テストでは検出されない問題である。

対応済みの項目は本ファイルから削除する（履歴は `git log` を参照）。

---

## 優先度: 中

### 合成・ロック

- **`synth.py:201`** キャッシュ書き込みが非アトミック。並行プロセスが
  `st_size > 44` を満たす書きかけの wav を読み、途中までの音声を再生し得る。
  同一ディレクトリの一時名へ書いてから `os.replace` する
- **`synth.py:276`** `_simple_run` が終了コードを無視するため、`list_narrators()` が
  失敗時のエラーメッセージを声の一覧として返す
- **`synth.py:181`** リトライ戦略が粗い。`retries=3` で同じ fallback モードを
  バックオフなしで 3 連続試行し、元のモードは再試行されない。一過性の失敗と
  文字数超過のような恒久的失敗も区別していない

### ログ

- **`synth.py:233`** `log.debug("実行: %s", command)` の `command` は `input_mode="say"` の
  とき本文を含むため、`log.level=debug` でアシスタント応答の全文が平文で蓄積される。
  長さのみ、または先頭数文字＋ハッシュにする
- **`logging_util.py:53`** hook プロセスとデタッチされた読み上げプロセスが同一ファイルへ
  同時に書くが、`RotatingFileHandler` はプロセス間ロックを持たない

### wav

- **`wavutil.py:55`** `except wave.Error` では壊れた wav の例外を捕捉しきれない
  （実測で `RuntimeError` が素通り。3.9 では `EOFError` も想定）。
  `pipeline.py:195` も `KeyboardInterrupt` しか捕捉していない
- **`wavutil.py:28`** docstring は「1 件でも読めれば dest を返す」だが、実際は 1 件でも
  異常があれば `PlayerError` で中断し、中途半端な dest が残る
- **`wavutil.py:18`** `wav_duration()` がヘッダの `nframes` だけを見るため、
  truncated wav に対して嘘の秒数を返す（`player.py:179` の再生時間計算がずれる）

### その他

- **`transcript.py:74`** 直近 400 件を見るためだけに JSONL 全体をメモリへ読む。
  `deque(iter_entries(path), maxlen=max_lookback)` に置き換える。
  78 行の `entries[-max_lookback:]` は `max_lookback=0` のとき全件になるためガードを

---

## 優先度: 低

- **`logging_util.py:50`** `log.level=off` でもログファイルとディレクトリが作られる
- **`synth.py:129`** 一時領域が `/mnt/c/Windows/Temp` になった場合、`cache/<sha1>.wav` が
  予測可能な名前で他ユーザから書き換え可能な場所に置かれる
- **`wavutil.py:50`** 無音挿入の判定が `path is not usable[-1]` という同一性比較。
  無音バイト数もフレーム境界に揃わない可能性がある
- **`wavutil.py:21,39,67`** `contextlib.closing` は不要（`wave.open` は 3.4 以降
  コンテキストマネージャ対応）
- **`pipeline.py:139`** 空入力時に `report.elapsed` が設定されず `summary()` が不正確
- **`pyproject.toml`** `[project.urls]` と `Operating System ::` /
  `Programming Language :: Python :: 3.9` 系の細目 classifier が無い。
  `license = { file = "LICENSE" }` と `License ::` classifier の併用は
  setuptools>=77 で非推奨（`license = "MIT"` + `license-files` へ）
- **`examples/settings.json:2`** トップレベルの `"$comment"` は Claude Code の settings
  スキーマが警告する可能性がある（未検証）。コピー元として配布するファイルなので
  コメントは README 側に置く方が安全
- **`tests/test_transcript.py:83`** `tempfile.mkstemp(...)[1]` が fd をリークし、
  テンポラリファイルも削除されない

---

## テストの穴

以下は 1 件も検証されていない（2026-09-15 時点、テストは 282 件）。

### 未検証のモジュール

- **`synth.py`** — `tests/test_synth.py` は作業ディレクトリの扱いのみ。キャッシュ命中、
  破損キャッシュのフォールバック、`prune_cache`、モードのフォールバック順、
  タイムアウト、先頭 `-` の並べ替え、`_decode` の cp932 経路が未検証
- **`wavutil.py` に専用テストが無い** — フォーマット不一致、`gap_seconds`、
  読めないソース、空リスト、`write_silence`。`write_silence()` で異フォーマットの wav を
  生成すれば依存を足さずに単体テストが書ける

### 未検証の分岐

- **CLI** — `speak --player`、`speak --concat`
- **`transcript.py`** — `content` が None / 非リスト、`isMeta`、`role != "assistant"`、
  JSON として妥当だが dict でない行、空ファイル、`max_lookback`

### `fake_voicepeak.py` の拡張余地

ハング（タイムアウト検証用）、cp932 出力、ロックファイルによる同時起動検出を
実装すれば、`ExeLock` の直列化と `_run` のタイムアウト処理をエンドツーエンドで
検証できる。

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
