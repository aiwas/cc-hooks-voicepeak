# CLAUDE.md

このリポジトリで作業するときの技術メモ。使い方は [README.md](README.md) を参照。

## 開発コマンド

```bash
python3 -m unittest discover -s tests -t .   # テスト全件 (159 件)
./bin/cc-voicepeak check --notes             # WSL 連携の注意点
./bin/cc-voicepeak split -f notes.md         # 分割結果だけ確認 (合成しない)
./bin/cc-voicepeak -v speak "テスト" --dry-run   # -v はサブコマンドより前
```

依存パッケージは追加しない方針（Python 3.9+ 標準ライブラリのみ）。
`tests/fake_voicepeak.py` が本物と同じオプションを受け取り、140 文字超でエラーを返す
ダミーとして動作するため、Windows も VOICEPEAK も無い環境で分割・直列合成・キャッシュ・
hook 経路まで通しで検証できる。

## 全体の流れ

```
┌─ WSL (Claude Code) ─────────────────────────┐   ┌─ Windows ──────────────┐
│                                             │   │                        │
│  Stop hook ──▶ cc-voicepeak hook            │   │                        │
│                  │ (即 exit 0)              │   │                        │
│                  ▼                          │   │                        │
│           読み上げプロセス (別プロセス)      │   │                        │
│              1. transcript.jsonl から本文    │   │                        │
│              2. Markdown を整形              │   │                        │
│              3. 140 文字以内に字句分割       │   │                        │
│              4. ブロックごとに ─────────────────▶ voicepeak.exe -s ... -o │
│                 直列で合成                   │   │      (1回140文字まで)   │
│              5. できた wav を順次 ──────────────▶ powershell.exe          │
│                 流し込んで再生                │   │   SoundPlayer.PlaySync │
└─────────────────────────────────────────────┘   └────────────────────────┘
```

## なぜこの構成なのか

VOICEPEAK のコマンドラインには 2 つの厳しい制約がある。

1. **1 回の実行で 140 文字（日本語換算）まで** — 141 文字以上を渡すとエラーになり
   wav が出力されない
2. **外部連携が EXE 実行のみ** — 常駐 API は無く、1 ブロックごとに EXE を起動する
   しかない（同時起動も不可）

したがって長文の読み上げは「字句解析して 140 文字以内のブロックに分割 → 直列に音声変換 →
連続再生」という手順が必須になる。このリポジトリはその手順を、WSL から Windows の EXE を
呼び出すための面倒事（パス変換・cwd・音声出力）ごとまとめて実装したもの。

## モジュール構成

| ファイル | 役割 |
|---|---|
| `cc_voicepeak/cli.py` | argparse とサブコマンドの実装。エントリポイントは `main()` |
| `cc_voicepeak/hook.py` | Hook ペイロードの解釈とデタッチ起動 |
| `cc_voicepeak/transcript.py` | `transcript.jsonl` から最終応答を抽出 |
| `cc_voicepeak/normalize.py` | Markdown → 読み上げ用テキスト |
| `cc_voicepeak/splitter.py` | 字句解析＋140 文字パッキング |
| `cc_voicepeak/synth.py` | voicepeak.exe の直列実行・キャッシュ・リトライ |
| `cc_voicepeak/player.py` | 常駐 PowerShell プレイヤ / WSL 側コマンド |
| `cc_voicepeak/pipeline.py` | 整形→分割→合成→再生の接続 |
| `cc_voicepeak/bridge.py` | WSL↔Windows のパス変換・EXE 探索・一時領域 |
| `cc_voicepeak/locking.py` | EXE 直列化と割り込み制御 |
| `cc_voicepeak/config.py` | 設定のマージと検証。既定値は `DEFAULTS` |
| `cc_voicepeak/diagnose.py` | `check` サブコマンドの中身 |
| `cc_voicepeak/logging_util.py` | ローテーション付きログ |
| `cc_voicepeak/wavutil.py` | wav の連結（`player.concat`） |

## WSL から Windows の EXE を実行する

ここが一番はまるところなので、実装で踏んだ対策を挙げておく。
`cc-voicepeak check --notes` でも同じ内容が出る。

### 1. WSL interop が前提

`.exe` を直接実行できるのは `binfmt_misc` に `WSLInterop` が登録されているとき。

```ini
# /etc/wsl.conf
[interop]
enabled=true
appendWindowsPath=true
```

書き換えたら PowerShell で `wsl --shutdown` してから入り直す。
確認は `cat /proc/sys/fs/binfmt_misc/WSLInterop`（`enabled` と出る）。
`appendWindowsPath=false` にしていると `powershell.exe` / `taskkill.exe` も PATH から
消えるので、再生と中断ができなくなる。判定は `bridge.interop_enabled()`。

### 2. EXE 自体は Linux パスで起動できる

```bash
'/mnt/c/Program Files/VOICEPEAK/voicepeak.exe' --help
```

`subprocess` からも同じで、`argv[0]` は `/mnt/...` のままでよい。
日本語を含む引数（`-s "…"`）は interop 層が UTF-8 → UTF-16 変換するため、
そのまま渡して問題ない。

### 3. 引数の *パス* は Windows 形式に変換する

`-o /mnt/c/tmp/a.wav` は Windows 側から見えないので `wslpath -w` で変換する。
ただし `/mnt/<drive>/...` は文字列置換で足りるため、`WslBridge.to_win()` が
`/mnt/c/x` → `C:\x` を自前で処理し、それ以外のパスのときだけ `wslpath` を呼ぶ
（結果はキャッシュ）。ブロックごとにサブプロセスを増やさないための最適化。

### 4. cwd は必ず `/mnt/...` 配下にする

WSL 側のパスを cwd にして Windows EXE を起動すると
`UNC パスはサポートされません。既定のディレクトリとして Windows ディレクトリを使用します`
と警告され、cwd が `C:\Windows` に差し替わる。`-o` を相対パスにしていると
`C:\Windows\output.wav` に書こうとして権限エラーになる。
そのため cwd を作業ディレクトリ（`/mnt/c/.../Temp/cc-voicepeak`）に固定し、
`-o` も常に絶対パスで渡す。`Bridge.exec_cwd()` は作業ディレクトリが `/mnt/` 配下でない
場合に `/mnt/c` へフォールバックする。

### 5. wav は Windows ファイルシステム上に出す

`\\wsl.localhost\...`（9p 経由）への書き込みは遅く、環境によっては失敗する。
既定では Windows の `%TEMP%\cc-voicepeak` を使う。`%TEMP%` は初回だけ
`cmd.exe /d /c echo %TEMP%` で取得して `~/.cache/cc-voicepeak/wintemp` にキャッシュする。
候補列挙（`_windows_temp_candidates()`）はジェネレータなので、キャッシュが使える限り
`cmd.exe` は起動しない。
明示する場合は `CC_VOICEPEAK_TEMP=/mnt/c/temp/cc-voicepeak`。
Windows の TEMP がどうしても見つからないときだけ、最後の手段として
`$TMPDIR`（既定 `/tmp`）配下に落ちる（遅いうえ環境によっては失敗する）。

### 6. 音を鳴らすのも Windows 側にやらせる

WSL には既定でサウンドデバイスが無い。そこで **`powershell.exe` を 1 つだけ常駐させ、
標準入力から wav のパスを 1 行ずつ受け取って `System.Media.SoundPlayer.PlaySync()` で
再生させる**。

```powershell
$player = New-Object System.Media.SoundPlayer
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $player.SoundLocation = $line; $player.Load(); $player.PlaySync()
}
```

この形にすると、

- `powershell.exe` の起動コスト（0.5 秒程度）を 1 回で済ませられる
- **合成できたブロックから順に流し込める**（全部の合成を待たずに再生が始まる）
- ブロック境界の切れ目が `PlaySync` の連続で詰まる

スクリプトは `-EncodedCommand`（UTF-16LE + Base64）で渡しているので、
実行ポリシーや引数クォートの問題を踏まない。

実行ファイルは `player.find_powershell()` が `powershell.exe` → `pwsh.exe` の順に
PATH を探し、無ければ `POWERSHELL_FALLBACKS` の既定インストール先を見る
（`appendWindowsPath=false` でも動くように）。それでも見つからなければ
`backend="auto"` は WSL 側の `paplay` などへ落ちる。無言で無音にはしない。
WSLg で PulseAudio が使える環境なら `player.backend` に `paplay` / `aplay` / `ffplay`
も選べる（`CommandPlayer`。WSL 側で完結する）。

### 7. 止めるときは `taskkill.exe`

Linux 側のプロセスを kill しても Windows 側の子が残ることがある。
そのため常駐プレイヤは起動直後に自分の `$PID` を標準出力へ返し、こちらはそれを
記録しておいて `taskkill.exe /PID <pid> /T /F` で確実に止める（`locking.taskkill()`）。
これにより「前のタスクの読み上げ中に次のタスクが終わった」ときの割り込み
（`hook.on_busy = replace`）が機能する。

## 長文の分割

`normalize` → `split` の 2 段。

### 読み上げ用の整形（`normalize.py`）

Claude の応答は Markdown なので、そのまま読ませると聞き取れない。

| 入力 | 読み上げ |
|---|---|
| ` ```python\nprint(1)\n``` ` | 「コードブロック。」（`code_blocks` で `drop` / `read` も選べる） |
| `## 実装完了` | 「実装完了。」 |
| `**重要**な点` | 「重要な点」 |
| `[README](https://…)` | 「README」 |
| `https://example.com/x` | 「リンク」 |
| `src/cc/splitter.py:120` | 「splitter.py の120行目」 |
| `- [x] 分割` | 「完了、分割」 |
| 表・水平線・絵文字 | 落とす |
| `<div>tag</div>` | 「tag」（タグだけ外して中身は読む） |
| `PR をマージ` | 「プルリクをマージ」（`normalize.replacements` の読み替え辞書） |

読み替え辞書の既定値は `(?i)\bPR\b` のように `\b` を使っているため、
**`PRをマージ` のように日本語と直接つながっている場合はマッチしない**
（Python の `\b` は日本語文字も語中文字として扱う）。日本語に挟まれた略語も
読み替えたい場合は `(?i)PR` のように `\b` を外したパターンを設定する。

`normalize()` を直接呼ぶときの既定は `DEFAULT_OPTIONS` で、`replacements` は空。
設定ファイル側の既定値（`config.DEFAULTS`）とは別物なので、両方を直す必要がある。

### 字句解析して 140 文字以内へ（`splitter.py`）

単純な 140 文字ごとのカットは文や単語の途中で切れて不自然になる。そこで次の手順を踏む。

1. **候補列挙**（`find_break_points()`）— 文字列を走査して「切ってよい位置」に優先度を付ける

   | 定数 | 優先度 | 位置 |
   |---|---:|---|
   | `PRIO_PARAGRAPH` | 100 | 空行（段落境界） |
   | `PRIO_SENTENCE` | 90 | `。．！？!?｡` の直後（`」』）` などの閉じ括弧まで含める） |
   | `PRIO_NEWLINE` | 80 | 単独の改行 |
   | `PRIO_COMMA` | 70 | `、，,；;` の直後 |
   | `PRIO_MIDDOT` | 64 | `：:…―‐—〜~` の直後 |
   | `PRIO_BRACKET_CLOSE` / `_OPEN` | 60 / 58 | 閉じ括弧の直後 / 開き括弧の直前 |
   | `PRIO_CONJ` | 46 | 接続助詞・活用語尾（`〜ので` `〜から` `〜けれど` `〜ました` `〜です`） |
   | （`PRIO_CONJ - 4`） | 42 | `て/で/が/し/ば` の直後 ※後続がひらがなでない場合 |
   | `PRIO_PARTICLE` | 38 | 格助詞の直後（`〜を` `〜に` `〜は` `〜も` `〜と` `〜まで` ほか）※後続がひらがなでない場合 |
   | `PRIO_SCRIPT` | 26 | 文字種の切り替わり（漢字／ひらがな／カタカナ／英数） |
   | `PRIO_SPACE` | 20 | 空白の直後 |
   | `PRIO_ANY` | 8 | 禁則を満たす任意位置 |

   同じ位置に複数の候補が立つ場合は最高優先度だけを残す（`offer()`）。
   後続がひらがなのときに 42 / 38 を出さないのは、「〜して / いる」「〜に / ついて」の
   ように助詞や補助用言の途中で切れるのを避けるため。

2. **貪欲パッキング**（`split_text()`）— 先頭から、上限に収まる範囲のうち
   `min_fill`（既定 0.55、つまり 77 文字）を超えた位置にある候補の中で最も優先度の
   高いものを選ぶ。「ブロックを十分詰めて EXE 起動回数を減らす」と「自然な位置で切る」
   の両立。`min_fill` を上げれば起動回数が減り、下げれば区切りが自然になる。

3. **禁則処理付きハードカット**（`_hard_cut()`）— 候補が皆無な場合（記号の無い長い
   文字列など）のみ。行頭禁則（`、。」）ー` 小書き仮名など）、行末禁則（`「（`）、
   英単語・数値・パスの内部、結合文字・異体字セレクタ・ZWJ 絵文字連結の内部では切らない。

4. **末尾の均し**（`_balance_tail()`）— 最後のブロックが極端に短くなったら、
   直前のブロックとまとめて再分割。

### 合成と再生

- ブロックごとに `voicepeak.exe -s <text> -o <win path>` を **直列**に起動
  （同時起動不可のため、ホスト全体で `flock` を取る。`locking.ExeLock`）
- 成功した wav を即プレイヤへ流し込む（`player.concat: true` なら `wavutil` で
  1 本に連結してから再生。無音の継ぎ目が完全に消える）
- 同じ文面＋同じ声の wav は SHA-1 キーでキャッシュ再利用（`voicepeak.cache`）。
  「テストは全部で〜」のような定型句が多い Claude の応答では効果が出やすい
- `-s` で失敗したブロックは `-t <file>`（UTF-8・BOM 無し）で自動リトライ

## Hook の動作

- `cc-voicepeak hook` は stdin の JSON ペイロードを読み、`hook.events` に含まれる
  イベントだけを処理する
- `Stop` は `transcript_path` の末尾からアシスタント応答を拾う（`transcript.py`）
- 読み上げ本体は `setsid` 済みの別プロセスへ渡して即 `exit 0`（`hook.spawn_detached()`）。
  子プロセス側は `CC_VOICEPEAK_DETACHED` が立った状態で動く。
  `--config` / `-v` / `--log-level` は `cc-voicepeak` 直下のオプションなので
  サブコマンド名の **前**、`-n` などの声の指定は `speak` のオプションなので **後ろ**に
  並べる（順序を間違えると argparse が `unrecognized arguments` で終了する）
- セッションごとの状態は `SpeechSlot`（ランタイムディレクトリの JSON）に持ち、
  `hook.on_busy` に応じて `replace`（前を taskkill して割り込む）/ `queue`（待つ）/
  `skip`（捨てる）を切り替える
- **読み上げの失敗で Claude Code の作業を止めない。** `cli.main()` は
  `KeyboardInterrupt` 以外のすべての例外を捕まえ、`hook` サブコマンドのときは 0 を返す。
  `--sync` / `hook.detach: false` の経路も、`cmd_speak()` が非 0 を返したら警告ログに
  落として 0 で終わる。`hook` 以外のサブコマンドは通常どおり 1 を返す

## 設定の仕組み

`config.py` の `DEFAULTS` が設定側の既定値定義（`normalize.DEFAULT_OPTIONS` とは別）。
読み込み順は README の通りで、`_merge_files()` がファイルを `_deep_merge()` で再帰
マージし、`_ENV_MAP` の環境変数、`--config` のファイル、最後に CLI 引数を重ねる。
`--config` を環境変数より後に置いているのは、これがコマンドライン引数だから。
プロジェクト設定の探索は `CLAUDE_PROJECT_DIR` があればそこだけ、無ければ cwd。

`_INT_KEYS` に入っているキーは、環境変数・CLI 引数・**設定ファイル**のいずれから
来ても `_coerce_known_keys()` で int に変換される（変換できなければ `ConfigError`）。
`_validate()` が `voicepeak.char_limit <= 140` などの不変条件を検査する。
`DEFAULTS` に無いキーは `unknown_keys()` が拾い、`check` が WARN として表示する
（読み込み自体は通る）。

設定キーを追加するときは、`DEFAULTS` に既定値を書き、必要なら `_ENV_MAP` /
`_INT_KEYS` と `_validate()`、README の設定表も合わせて更新する。

## 実装上の注意

- `voicepeak.char_limit` は 140 を超えられない。上限を上げる変更は入れない
- voicepeak.exe は同時起動できない。合成は必ず `ExeLock` の下で直列に実行する
- Windows に渡すパスは常に `Bridge.to_win()` を通し、絶対パスにする
- `wslpath` の呼び出しを増やさない（ブロックごとのサブプロセス起動はコストが大きい）
- 新しい分割ルールを足したら `tests/test_splitter.py` に境界のケースを追加する
- テストは実際の voicepeak を呼ばない。`tests/fake_voicepeak.py` を経由させる
