# TODO / 改善候補

コードレビューで洗い出した項目。優先度順。
「[確認済]」は手元で実際に再現・実測したもの、それ以外はコードを読んで指摘したもの。
現時点でテストは 134 件すべて成功しているため、以下はいずれも既存テストでは
検出されない問題である。

---

## 優先度: 高

### 1. 長文で読み上げ開始が数十秒遅れる（O(n²)）— `splitter.py:345`

[確認済] `split_text()` がループのたびに残りの全文へ `find_break_points()` を実行する。

| 入力 | 所要時間 |
|---:|---:|
| 2,000 文字 | 0.10 秒 |
| 10,000 文字 | 2.55 秒 |
| 30,000 文字 | 23.16 秒 |

`normalize.max_total_chars` の既定は 0（無制限）なので、長い応答でそのまま遅延する。

候補列挙を上限ウィンドウ（`remaining[:max_index]` + 接尾辞判定用の数文字）に限定するか、
全文で 1 回だけ列挙してオフセットを引きながら再利用する。

### 2. `_endswith_any()` が呼び出しごとに `sorted()` する — `splitter.py:183`

1 文字あたり 2 回呼ばれる。`CONJ_SUFFIXES` / `PARTICLES` を長さ降順に並べた
モジュール定数として持ち、関数内は走査のみにする。1 と併せて対応する。

### 3. ロックを取得できなくても合成を続行する — `locking.py:56`

`ExeLock` はタイムアウト時に警告を出して `return self` するため、
**voicepeak.exe が同時起動される**（同時起動不可という前提が破れる）。
`__exit__` も保持していないロックに対して `LOCK_UN` を呼ぶ。

取得成否を属性に保持し、未取得なら合成をスキップして `SynthError` にする。
`tests/test_locking.py:131` の `test_lock_timeout_does_not_raise` は
「例外が出ないこと」しか見ておらず、この危険な挙動を仕様として固定してしまっている。

### 4. hook が終了コード 1 で落ちる経路がある — `cli.py:400`

[確認済] `main()` は `CcVoicepeakError` しか捕捉しないため、`ValueError` / `OSError` は
素通りしてトレースバックのまま exit 1 になる。`bin/cc-voicepeak:31` は `exec` するので
ランチャ側でも救えない。

再現: `.claude/voicepeak.json` に `{"hook":{"min_chars":"x"}}` を置いて `hook --sync`
→ `cli.py:293` の `int()` が `ValueError` → exit 1。

`main()` で `KeyboardInterrupt` 以外の例外も捕捉し、`args.command == "hook"` のときは
必ず 0 を返す。

### 5. デタッチ起動でボイス指定が黙って捨てられる — `hook.py:90`

[確認済] `spawn_detached()` が組み立てるコマンドは
`speak --stdin --session --on-busy --config` のみ。`hook` サブコマンドに定義されている
`-n/-e/--speed/--pitch/--exe`（`cli.py:94`）と `-v/--log-level` が子プロセスに渡らない。
`hook.detach` の既定は true なので、`--sync` を付けない限り常に無視される。
`--exe` は子プロセス側で解決できなくなるため影響が大きい。

### 6. 設定ファイル由来の値が型変換されない — `config.py:289`

`_coerce()` は環境変数と CLI override にしか適用されず、JSON 由来の値は素通し。
`{"voicepeak":{"speed":"fast"}}` で `int(speed)` が生の `ValueError` を送出する（4 の経路）。
`_validate()` 内の `int()` を `ConfigError` に変換するか、`_deep_merge` 後に
`_INT_KEYS` へ `_coerce` を適用する。

### 7. インラインコード内のスネークケースが壊れる — `normalize.py:184`

[確認済] 強調記号の除去がインラインコード展開より前に走るため、`_` と `*` を巻き込む。

| 入力 | 現状の出力 |
|---|---|
| `` `get_last_assistant_text` `` | `getlastassistant_text` |
| `max_total_chars_value` | `maxtotalcharsvalue` |
| `__init__` | `_init__` |
| `2*3*4` | `234` |

`_INLINE_CODE` を先に処理して中身をプレースホルダで保護する。加えて `_` の強調は
前後が単語構成文字でない場合に限定する。

### 8. パスでない文字列がパス扱いされ前半が消える — `normalize.py:34`

[確認済] `_PATHISH` の `\w` が Unicode 対応のため、日本語や日付を巻き込む。

| 入力 | 現状の出力 |
|---|---|
| `2024/09/14 に実施` | `14 に実施` |
| `読み/書き の権限` | `書きの権限` |
| `AND/OR` | `OR` |

`(?:[\w.\-]+/){1,}` の分岐を ASCII に限定し、末尾要素が既知の拡張子を持つ場合のみ
短縮する。

### 9. 閉じられていないコードフェンス以降の本文が消える — `normalize.py:95`

[確認済] `前。\n```py\ncode()\nまだ続く本文です。` → `前。\nコードブロック。`。
フェンスの数が奇数になる出力（フェンスを含むコード例、途中で切れた応答）で発生する。
フェンス内の行をバッファし、EOF 時点で未クローズなら本文として復帰させる。

### 10. `_balance_tail()` が区切りなしで連結する — `splitter.py:386`

`("" if blocks[-2].endswith("\n") else "")` は両分岐とも空文字で、区切り挿入の意図が
失われている。`split_text` が境界で `lstrip()` した空白が復元されず、
`"word"` + `"tail"` → `"wordtail"` のように単語が繋がる。

### 11. 割り込み時に作業ディレクトリが必ず残る — `synth.py:128` / `pipeline.py:208`

`work_dir = <temp>/run/<pid>` の削除は `pipeline.speak` の `finally` にしかない。
既定の `on_busy=replace` では対象プロセスが SIGKILL されるため実行されず、
Windows の `%TEMP%\cc-voicepeak\run\<pid>\*.wav` が無制限に蓄積する。
起動時に古い `run/*` を掃除する処理は存在しない。

### 12. stale / 再利用された PID のプロセスグループを killpg する — `locking.py:157`

生存判定が `os.kill(pid, 0)` のみ。PID 再利用時に無関係なプロセス群を停止させ得る。
状態ファイルに `/proc/<pid>/stat` の starttime か cmdline の照合値を記録し、
一致した場合のみ kill する。

---

## 優先度: 中

### プロセス・再生

- **`player.py:198`** `stop()` が `wait()` を呼ばずに参照を捨てるためゾンビが残り、
  `stdin`/`stdout` の fd もリークする。`kill()` → `wait(timeout=5)` → パイプ `close()`
  → reader スレッドの `join` まで行う
- **`player.py:142`** PID 待ちが 20 秒ブロックし、powershell.exe が即死しても
  `_win_pid=None` のまま成功扱いで戻る。以降 `taskkill` による割り込みが不可能になる。
  待機ループ内で `self.proc.poll()` を確認する
- **`player.py:265`** `CommandPlayer` に再生タイムアウトが無い。`finish()` は
  `join(timeout=None)`、ワーカ内の `wait()` にも timeout が無く、`aplay` がデバイス待ちで
  停止すると常駐プロセスが永久に残る
- **`player.py:293`** `auto` かつ WSL で `powershell.exe` の存在を確認せず選択する
  （`diagnose.py:130` は確認しているのに実行経路は見ていない）。`appendWindowsPath=false`
  の環境では無音のまま合成だけが進む。`pwsh.exe` や絶対パスへのフォールバックを
- **`player.py:281`** `player.volume` が powershell バックエンドに渡らず、WSL 既定経路では
  設定しても効果がない。`SoundPlayer` では音量制御できない旨を `check` で警告する
- **`player.py:244`** ワーカスレッドが `PlayerError` で静かに停止し、以降の `enqueue()` が
  無反応になる

### 合成・ロック

- **`synth.py:201`** キャッシュ書き込みが非アトミック。並行プロセスが
  `st_size > 44` を満たす書きかけの wav を読み、途中までの音声を再生し得る。
  同一ディレクトリの一時名へ書いてから `os.replace` する
- **`synth.py:276`** `_simple_run` が終了コードを無視するため、`list_narrators()` が
  失敗時のエラーメッセージを声の一覧として返す
- **`synth.py:181`** リトライ戦略が粗い。`retries=3` で同じ fallback モードを
  バックオフなしで 3 連続試行し、元のモードは再試行されない。一過性の失敗と
  文字数超過のような恒久的失敗も区別していない
- **`locking.py:118`** 状態ファイルの一時名 `speech-<key>.tmp` を同一セッションキーの
  全プロセスが共有し、`update()` は `write_text` で直接上書きする。書きかけを読むと
  `read()` が `None` を返し「スロット空き」と誤認される
- **`locking.py:80`** taskkill の returncode を検査せず、`interrupt()` は無条件に
  `killed = True` にする
- **`cli.py:216`** busy 判定と `slot.write()` の間に TOCTOU があり、
  `wait_until_free()` が False を返してもそのまま進むため読み上げが重なる

### 設定・CLI

- **`cli.py:407`** `CLAUDE_PROJECT_DIR` があるだけで全コマンドのエラーが 0 になる。
  hook 判定は左辺だけで足りる
- **`config.py:233`** `--config` で渡したファイルが環境変数に負ける
  （「CLI 引数が最優先」という規約と不整合）
- **`config.py:207`** cwd の `.claude/voicepeak.json` が `CLAUDE_PROJECT_DIR` 側より
  優先される。README には明記済みだが、`CLAUDE_PROJECT_DIR` があるときは cwd 候補を
  足さない方が素直
- **`config.py:273`** バリデーション漏れ。未検証: `split.width_mode` /
  `normalize.code_blocks` / `inline_code` / `tables` / `log.level` / `hook.events` の型 /
  `voicepeak.timeout`（0・負値）/ `retries` / `cache_max_files` / `hook.min_chars` /
  `normalize.max_total_chars`（負値）
- **`config.py:181`** 未知キー・タイポが黙って通る。`narator` のような綴り誤りが無反応。
  `DEFAULTS` に無いキーを `check` で WARN として列挙したい
- **`cli.py:371`** `install-hook` が常に `$CLAUDE_PROJECT_DIR/bin/...` を出力する。
  `pip install -e .` 経由では `bin/` が無く動かない。`--installed` 相当のオプションを
- **`diagnose.py:129`** `backend="auto"` かつ非 WSL で、detail も hint も空の WARN 行に
  なる。paplay/aplay/ffplay の実在チェックを行う

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
- **`normalize.py:146,236`** 箇条書き行末に付けた「、」が `_collapse()` の
  `rstrip("、,")` で必ず除去され、意図したポーズが入らない（実質デッドコード）
- **`normalize.py:190`** `_HTML_TAG` が `List<int>` のような型表記を削除する

---

## 優先度: 低

- **`cli.py:173`** `split --json` は空入力でも 0、非 JSON 分岐は 1。終了コードを揃える
- **`cli.py:154`** `-f` のファイル不在で `FileNotFoundError` のトレースバックが出る
- **`logging_util.py:50`** `log.level=off` でもログファイルとディレクトリが作られる
- **`diagnose.py:229`** `powershell_available()` がどこからも参照されていない
- **`hook.py:36`** `read_payload` が stdin tty のとき無限待ちになる。
  手動で `cc-voicepeak hook` を叩くと固まる
- **`hook.py:118`** 分離プロセスへの stdin 書き込みがパイプバッファ（通常 64KB）超で
  ブロックし得る。`install-hook` が案内する `timeout: 10` を超過する可能性がある
- **`locking.py:31`** ランタイムディレクトリのパーミッションが umask 任せ。
  `XDG_RUNTIME_DIR` 未設定時は `/tmp/cc-voicepeak` になり、状態ファイル（読み上げテキスト
  先頭 80 文字を含む）が他ユーザから読める。`mkdir(mode=0o700)` を明示する
- **`cli.py:247`** 上記の状態ファイルに本文が平文で残る。長さだけに落とすか `0o600` を
- **`synth.py:128`** `work_dir` が PID のみで一意化されており、PID 再利用で衝突する
- **`synth.py:129`** 一時領域が `/mnt/c/Windows/Temp` になった場合、`cache/<sha1>.wav` が
  予測可能な名前で他ユーザから書き換え可能な場所に置かれる
- **`bridge.py:209`** wintemp キャッシュが有効でも毎回 `cmd.exe` を起動する
  （`_query_windows_env` が候補リスト構築時に必ず走る）。遅延評価にする
- **`bridge.py:62`** interop 判定が `"enabled" in <ファイル全体>` の部分一致。
  先頭行の完全一致にする
- **`bridge.py:313`** `resolve_exe` が UNC パス（`\\wsl.localhost\...`）を変換せず、
  実行権限も確認しない
- **`bridge.py:191,103`** `CC_VOICEPEAK_TEMP` の評価タイミングが `LocalBridge` と
  `WslBridge` で不統一
- **`normalize.py:155`** `if v is not None` のため、設定で明示的に null を指定しても
  既定値に戻る
- **`normalize.py:19`** `_BLOCKQUOTE` が 1 段しか除去せず、`> > 引用` の `>` が残る
- **`normalize.py:113`** 先頭パイプの無い区切り行（`---|---`）が読み上げ対象として残る
- **`normalize.py:50,67`** 絵文字判定の取りこぼしと過剰削除。U+20E3（囲み keycap）の
  結合記号が残る一方、矢印（0x2190-0x21FF）は削除されるため `入力 → 出力` が
  `入力出力` になる。矢印は「から」等への置換候補にする方が自然
- **`normalize.py` 全般** 列挙値に検証が無く、`tables="???"` は `read` 相当、
  `code_blocks="???"` は `drop` 相当に無言で落ちる
- **`splitter.py:118`** 未知の `width_mode` が無言で `codepoints` と同じ動作になる
- **`splitter.py:185`** `startswith(suffix, pos - len(suffix))` を負インデックス判定より
  先に評価している（短絡で現状は誤検出しないが脆い）
- **`splitter.py:203`** 条件式の重複（`text[index-2] in ".·・ "` が右辺を包含）
- **`splitter.py:93`** `code == 0xFF70` は `0xFF66 <= code <= 0xFF9D` に含まれる冗長条件
- **`splitter.py:364`** `drop_empty` によるテキスト欠落がログにも残らない
- **`wavutil.py:50`** 無音挿入の判定が `path is not usable[-1]` という同一性比較。
  無音バイト数もフレーム境界に揃わない可能性がある
- **`wavutil.py:21,39,67`** `contextlib.closing` は不要（`wave.open` は 3.4 以降
  コンテキストマネージャ対応）
- **`normalize.py:151`** ここだけ `Dict[str, object] | None`（PEP 604）で、
  他モジュールの `Optional[...]` と不統一。`from __future__ import annotations` が
  あるため 3.9 でも実行時エラーにはならないが、3.9 で `typing.get_type_hints()` を
  呼ぶと失敗する
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

既存 134 件が通る状態でも、以下は 1 件も検証されていない。

### 未検証のモジュール

- **`synth.py` に専用テストが無い** — キャッシュ命中、破損キャッシュのフォールバック、
  `prune_cache`、モードのフォールバック順、タイムアウト、先頭 `-` の並べ替え、
  `_decode` の cp932 経路
- **`wavutil.py` に専用テストが無い** — フォーマット不一致、`gap_seconds`、
  読めないソース、空リスト、`write_silence`。`write_silence()` で異フォーマットの wav を
  生成すれば依存を足さずに単体テストが書ける
- **`PowershellPlayer` / `CommandPlayer` の実動作** — `tests/test_bridge.py:155` は
  スクリプト文字列とバックエンド選択のみ。PID 行のパース、`__QUIT__` 送出、
  `stop()` 後の状態、paplay の音量スケーリングが未検証。`fake_voicepeak.py` と同様の
  偽プレイヤスクリプトを置けば標準入力プロトコル全体を検証できる

### 未検証の分岐

- **hook の設定分岐一式** — `hook.min_chars` によるスキップ、`prefix`/`suffix`/
  `notification_prefix` の付加、`subagent` + `SubagentStop`、`detach: false`
- **デタッチ子プロセスへのオプション伝播** — `test_cli.py:199` は「子が合成したか」しか
  見ておらず、高 5 を検出できない。`hook -n <名前>` で narrator を検証するテストが有効
- **`on_busy` の 3 分岐**（`cli.py:213`）— `test_locking.py` は `SpeechSlot` 単体のみ
- **設定優先順位** — `CC_VOICEPEAK_CONFIG`、`--config`、cwd フォールバックが未検証
- **バリデーション** — `test_config.py:86` は char_limit / speed / player のみ。
  `pitch`・`on_busy`・`input_mode`・`min_fill`・トップレベルが dict でない JSON・
  設定ファイル内の非数値文字列（高 6）が未カバー
- **CLI** — `emotions`、`check --synth`、`install-hook --events`、`speak --player`、
  `speak --concat`、`--log-level`/`-v`
- **`bridge.py`** — `temp_root()` の候補選択、`_query_windows_env`、wintemp キャッシュの
  読み書き、`find_voicepeak` の Users/AppData 分岐（`Public`/`Default` 除外）
- **`locking.py`** — `update()`、`clear()` の他プロセス判定、taskkill 失敗時、
  `wait_until_free` が True を返す経路
- **`normalize.py`** — `code_blocks="read"`、`inline_code="drop"`、未クローズフェンス、
  空文字入力、Windows パス、入れ子引用、区切りマークの無い `max_total_chars` 切り詰め
- **`splitter.py`** — `limit<=0` の `ValueError`、`drop_empty=False`、`\r\n` 入力、
  サロゲートペアを含む文字列、`describe_blocks`、`char_width`
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
