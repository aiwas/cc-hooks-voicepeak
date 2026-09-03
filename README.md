# cc-hooks-voicepeak

Claude Code のタスクが終わったら、その結果を **VOICEPEAK** に読み上げさせる Hooks 一式。
Claude Code は WSL、VOICEPEAK は Windows という構成を前提にしている。

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

1. **1 回の実行で 140 文字（日本語換算）まで** — 141 文字以上を渡すとエラーになり wav が出力されない
2. **外部連携が EXE 実行のみ** — 常駐 API は無く、1 ブロックごとに EXE を起動するしかない（同時起動も不可）

したがって長文の読み上げは「字句解析して 140 文字以内のブロックに分割 → 直列に音声変換 →
連続再生」という手順が必須になる。このリポジトリはその手順を、WSL から Windows の EXE を
叩くための面倒事（パス変換・cwd・音声出力）ごとまとめて実装したもの。

依存パッケージは無し（Python 3.9+ 標準ライブラリのみ）。

## セットアップ

### 1. 取得と動作確認

```bash
git clone <this repo> ~/src/cc-hooks-voicepeak
cd ~/src/cc-hooks-voicepeak
./bin/cc-voicepeak check          # 環境診断
./bin/cc-voicepeak check --notes  # WSL 連携の注意点
```

`pip install -e .` でも入る（`cc-voicepeak` コマンドが生える）。入れなくても
`bin/cc-voicepeak` がそのまま使える。

### 2. voicepeak.exe を見つけさせる

既定で以下を自動探索する。

- `/mnt/{c,d,e}/Program Files/VOICEPEAK/voicepeak.exe`
- `/mnt/{c,d,e}/Program Files/AHS/VOICEPEAK/voicepeak.exe`
- `/mnt/c/Users/*/AppData/Local/Programs/VOICEPEAK/voicepeak.exe`

見つからない場合だけ明示する。

```bash
export CC_VOICEPEAK_EXE='/mnt/c/Program Files/VOICEPEAK/voicepeak.exe'
```

### 3. 声を選ぶ

```bash
./bin/cc-voicepeak narrators              # 使える声の一覧
./bin/cc-voicepeak emotions 'Miyamai Moca' # その声の感情パラメータ
./bin/cc-voicepeak speak "接続テストです。" -n 'Miyamai Moca' -e 'honwaka=40'
```

気に入った設定を `.claude/voicepeak.json` に書く（`examples/voicepeak.json` を参照）。

### 4. Hook を登録する

```bash
./bin/cc-voicepeak install-hook   # スニペットを表示
```

`.claude/settings.json`（プロジェクト単位）か `~/.claude/settings.json`（全体）にマージする。

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command",
                    "command": "$CLAUDE_PROJECT_DIR/bin/cc-voicepeak hook",
                    "timeout": 10 }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command",
                    "command": "$CLAUDE_PROJECT_DIR/bin/cc-voicepeak hook",
                    "timeout": 10 }] }
    ]
  }
}
```

- `Stop` … タスク完了時。`transcript_path` から最後のアシスタント応答を拾って読む
- `Notification` … 許可待ちや入力待ちの通知を読む
- `SubagentStop` … サブエージェント完了。読みたい場合は `hook.subagent` を `true` に

**hook は待たせない。** `cc-voicepeak hook` は読み上げ本体を別プロセス（`setsid` 済み）へ
投げて即 `exit 0` する。したがって `timeout` は短くてよく、読み上げが何十秒かかっても
Claude Code の会話は止まらない。voicepeak が見つからない等で失敗しても hook は 0 を返す
（読み上げの失敗で作業を止めない）。

## WSL から Windows の EXE を実行する

ここが一番はまるところなので、実装で踏んだ対策をすべて挙げておく。
`cc-voicepeak check --notes` でも同じ内容が出る。

### 1. WSL interop を有効にする

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
消えるので、再生と中断ができなくなる。

### 2. EXE 自体は Linux パスで起動できる

```bash
'/mnt/c/Program Files/VOICEPEAK/voicepeak.exe' --help
```

`subprocess` からも同じで、`argv[0]` は `/mnt/...` のままでよい。
日本語を含む引数（`-s "…"`）は interop 層が UTF-8 → UTF-16 変換してくれるため、
そのまま渡して問題ない。

### 3. 引数の *パス* は Windows 形式に変換する

`-o /mnt/c/tmp/a.wav` は Windows 側から見えない。`wslpath -w` で変換する。

```bash
wslpath -w /mnt/c/tmp/a.wav   # -> C:\tmp\a.wav
```

ただし `/mnt/<drive>/...` は文字列置換で足りるので、このツールでは
`WslBridge.to_win()` が `/mnt/c/x` → `C:\x` を自前で処理し、それ以外のパスのときだけ
`wslpath` を呼ぶ（結果はキャッシュ）。ブロックごとにサブプロセスを増やさないための最適化。

### 4. cwd は必ず `/mnt/...` 配下にする

WSL 側のパスを cwd にして Windows EXE を起動すると
`UNC パスはサポートされません。既定のディレクトリとして Windows ディレクトリを使用します`
と警告され、cwd が `C:\Windows` に差し替わる。`-o` を相対パスにしていると
`C:\Windows\output.wav` に書こうとして権限エラーになる。
本ツールは cwd を作業ディレクトリ（`/mnt/c/.../Temp/cc-voicepeak`）に固定し、
`-o` も常に絶対パスで渡す。

### 5. wav は Windows ファイルシステム上に出す

`\\wsl.localhost\...`（9p 経由）への書き込みは遅く、環境によっては失敗する。
既定では Windows の `%TEMP%\cc-voicepeak` を使う。`%TEMP%` は初回だけ
`cmd.exe /d /c echo %TEMP%` で取得して `~/.cache/cc-voicepeak/wintemp` にキャッシュする。
明示したい場合は `CC_VOICEPEAK_TEMP=/mnt/c/temp/cc-voicepeak`。

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
WSLg で PulseAudio が使える環境なら `player.backend` に `paplay` / `aplay` / `ffplay`
も選べる（WSL 側で完結する）。

### 7. 止めるときは `taskkill.exe`

Linux 側のプロセスを kill しても Windows 側の子が残ることがある。
そのため常駐プレイヤは起動直後に自分の `$PID` を標準出力へ返し、こちらはそれを
記録しておいて `taskkill.exe /PID <pid> /T /F` で確実に止める。
これにより「前のタスクの読み上げ中に次のタスクが終わった」ときの割り込み
（`hook.on_busy = replace`）が効く。

## 長文の分割

`normalize` → `split` の 2 段。

### 読み上げ用の整形（`cc_voicepeak/normalize.py`）

Claude の応答は Markdown なので、そのまま読ませると聞けない。

| 入力 | 読み上げ |
|---|---|
| ` ```python\nprint(1)\n``` ` | 「コードブロック。」（`code_blocks` で `drop` / `read` も選べる） |
| `## 実装完了` | 「実装完了。」 |
| `**重要**な点` | 「重要な点」 |
| `[README](https://…)` | 「README」 |
| `https://example.com/x` | 「リンク」 |
| `src/cc/splitter.py:120` | 「splitter.py の120行目」 |
| `- [x] 分割` | 「完了、分割」 |
| 表・水平線・HTML タグ・絵文字 | 落とす |
| `PR` | 「プルリク」（`normalize.replacements` の読み替え辞書） |

### 字句解析して 140 文字以内へ（`cc_voicepeak/splitter.py`）

単純な 140 文字ごとのカットは文や単語の途中で切れて不自然になる。そこで

1. **候補列挙** — 文字列を走査して「切ってよい位置」に優先度を付ける

   | 優先度 | 位置 |
   |---:|---|
   | 100 | 空行（段落境界） |
   | 90 | `。．！？` の直後（`」』）` などの閉じ括弧まで含める） |
   | 80 | 改行 |
   | 70 | `、，；` の直後 |
   | 64 | `：…―` の直後 |
   | 60 / 58 | 閉じ括弧の直後 / 開き括弧の直前 |
   | 46 | 接続助詞・活用語尾（`〜ので` `〜から` `〜けれど` `〜して` `〜が`） |
   | 38 | 格助詞の直後（`〜を` `〜に` `〜と`）※後続がひらがなでない場合 |
   | 26 | 文字種の切り替わり（漢字／ひらがな／カタカナ／英数） |
   | 8 | 禁則を満たす任意位置 |

2. **貪欲パッキング** — 先頭から、上限に収まる範囲のうち `min_fill`（既定 0.55、
   つまり 77 文字）を超えた位置にある候補の中で最も優先度の高いものを選ぶ。
   「ブロックを十分詰めて EXE 起動回数を減らす」と「自然な位置で切る」の両立。
   `min_fill` を上げれば起動回数が減り、下げれば区切りが自然になる。

3. **禁則処理付きハードカット** — 候補が皆無な場合（記号の無い長い文字列など）のみ。
   行頭禁則（`、。」）ー` 小書き仮名など）、行末禁則（`「（`）、
   英単語・数値・パスの内部、結合文字・異体字セレクタ・ZWJ 絵文字連結の内部では切らない。

4. **末尾の均し** — 最後のブロックが極端に短くなったら、直前のブロックとまとめて再分割。

分割結果は合成せずに確認できる。

```bash
$ ./bin/cc-voicepeak split -f notes.md
3 ブロック / 合計 312 文字 / 上限 140 文字
 [  1]  133 文字 | 実装が完了しました。まず設定ファイルを読み込む処理を追加し、…
 [  2]  133 文字 | voicepeak は一回の起動で百四十文字までしか受け付けないため、…
 [  3]   46 文字 | テストは全部で二十三件あり、すべて成功しています。…
```

### 合成と再生

- ブロックごとに `voicepeak.exe -s <text> -o <win path>` を **直列**に起動
  （同時起動不可のため、ホスト全体で `flock` を取る）
- 成功した wav を即プレイヤへ流し込む（`player.concat: true` なら
  `wave` モジュールで 1 本に連結してから再生。無音の継ぎ目が完全に消える）
- 同じ文面＋同じ声の wav は SHA-1 キーでキャッシュ再利用（`voicepeak.cache`）。
  「テストは全部で〜」のような定型句が多い Claude の応答では効きやすい
- `-s` で失敗したブロックは `-t <file>`（UTF-8・BOM 無し）で自動リトライ

## CLI

```
cc-voicepeak hook                     Claude Code Hooks から呼ばれる本体
cc-voicepeak speak "テキスト"          手動で読み上げ
cc-voicepeak speak -f notes.md -o out.wav   再生せず 1 本の wav に保存
cc-voicepeak split --stdin --json     分割結果だけ確認（合成しない）
cc-voicepeak check [--synth]          環境診断（--synth で実際に 1 回合成）
cc-voicepeak check --notes            WSL 連携の注意点
cc-voicepeak check --print-config     有効な設定を表示
cc-voicepeak narrators                声の一覧
cc-voicepeak emotions <名前>          感情パラメータの一覧
cc-voicepeak install-hook             settings.json スニペット
```

主なオプション: `-n/--narrator` `-e/--emotion` `--speed` `--pitch` `--limit`
`--min-fill` `--player` `--concat` `--no-normalize` `--no-cache` `--dry-run` `-v`

## 設定

読み込み順（後ろが優先）:

1. 組み込みデフォルト
2. `~/.config/cc-voicepeak/config.json`
3. `$CLAUDE_PROJECT_DIR/.claude/voicepeak.json`
4. `$CC_VOICEPEAK_CONFIG`
5. 環境変数（`CC_VOICEPEAK_EXE` `CC_VOICEPEAK_NARRATOR` `CC_VOICEPEAK_PLAYER` など）
6. コマンドライン引数

| キー | 既定 | 説明 |
|---|---|---|
| `voicepeak.exe` | 自動探索 | voicepeak.exe のパス |
| `voicepeak.narrator` / `.emotion` | なし | 声と感情（例 `happy=50,sad=0`） |
| `voicepeak.speed` / `.pitch` | なし | 50-200 / -300-300 |
| `voicepeak.char_limit` | 140 | 1 ブロックの上限。140 より大きくはできない |
| `voicepeak.input_mode` | `say` | `say`（`-s`）か `text_file`（`-t`） |
| `voicepeak.cache` | true | wav キャッシュ |
| `split.min_fill` | 0.55 | ブロックをどれだけ詰めるか |
| `split.width_mode` | `codepoints` | `halfwidth_half` にすると半角を 0.5 文字換算 |
| `normalize.code_blocks` | `placeholder` | `drop` / `read` |
| `normalize.max_total_chars` | 0（無制限） | 全体の上限。長すぎる応答を切る |
| `normalize.replacements` | 数件 | 読み替え辞書 `[正規表現, 読み]` |
| `player.backend` | `auto` | `powershell` / `paplay` / `aplay` / `ffplay` / `none` |
| `player.concat` | false | 連結してから再生 |
| `hook.events` | `["Stop","Notification"]` | 反応するイベント |
| `hook.on_busy` | `replace` | 読み上げ中に次が来たとき `replace` / `queue` / `skip` |
| `hook.min_chars` | 2 | これより短い応答は読まない |
| `hook.prefix` / `.suffix` | 空 | 定型句（例 `"お疲れさまです。"`） |
| `hook.detach` | true | hook を即 return させる |
| `log.level` | `info` | ログは `~/.local/state/cc-voicepeak/cc-voicepeak.log` |

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| 何も起きない | `cc-voicepeak check` → ログ `~/.local/state/cc-voicepeak/cc-voicepeak.log` を見る |
| `voicepeak.exe を実行できません` | interop が無効。`/etc/wsl.conf` を直して `wsl --shutdown` |
| `wav が出力されませんでした` | 出力先が Windows から書けない。`CC_VOICEPEAK_TEMP` を `/mnt/c/...` に |
| 音が鳴らない（合成は成功） | `powershell.exe` が PATH に無い。`appendWindowsPath=true` を確認。または `player.backend` を `paplay` に |
| 途中で不自然に切れる | `split.min_fill` を下げる（0.3 など）。`cc-voicepeak split` で境界を確認 |
| 長すぎて聞いていられない | `normalize.max_total_chars` を 300 程度に、または `hook.prefix` だけ読ませる運用に |
| 前の読み上げが終わらない | `hook.on_busy` を `replace`（既定）に。`skip` なら捨てる |
| 141 文字エラーが出る | `voicepeak.char_limit` を 130 程度に下げる（環境によって数え方が違う場合） |

## 開発

```bash
python3 -m unittest discover -s tests -t .
```

134 件。`tests/fake_voicepeak.py` が本物と同じオプションを受け取り、140 文字超で
エラーを返すダミーとして働くので、Windows も VOICEPEAK も無い環境（CI を含む）で
分割・直列合成・キャッシュ・hook 経路まで通しで検証できる。

| ファイル | 役割 |
|---|---|
| `cc_voicepeak/splitter.py` | 字句解析＋140 文字パッキング |
| `cc_voicepeak/normalize.py` | Markdown → 読み上げ用テキスト |
| `cc_voicepeak/bridge.py` | WSL↔Windows のパス変換・EXE 探索・一時領域 |
| `cc_voicepeak/synth.py` | voicepeak.exe の直列実行・キャッシュ・リトライ |
| `cc_voicepeak/player.py` | 常駐 PowerShell プレイヤ / WSL 側コマンド |
| `cc_voicepeak/pipeline.py` | 整形→分割→合成→再生の接続 |
| `cc_voicepeak/transcript.py` | `transcript.jsonl` から最終応答を抽出 |
| `cc_voicepeak/hook.py` | Hook ペイロードの解釈とデタッチ起動 |
| `cc_voicepeak/locking.py` | EXE 直列化と割り込み制御 |
| `cc_voicepeak/diagnose.py` | `check` の中身 |

## ライセンス

MIT
