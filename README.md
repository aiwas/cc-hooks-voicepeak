# cc-hooks-voicepeak

Claude Code のタスクが終わったら、その結果を **VOICEPEAK** に読み上げさせる Hooks 一式。
Claude Code は WSL、VOICEPEAK は Windows という構成を前提にしている。

- タスク完了（`Stop`）と通知（`Notification`）を読み上げる
- Markdown の応答を読み上げ向けに整形してから喋らせる
- VOICEPEAK の「1 回 140 文字まで」という制約を、自然な区切りでの自動分割で吸収する
- hook は即座に終了するので、読み上げが長くても Claude Code の会話は止まらない
- 依存パッケージなし（Python 3.13 以上の標準ライブラリのみ）
- Claude Code のプラグインとして入れられる（`settings.json` の編集が不要）

実装の詳細や設計の背景は [CLAUDE.md](CLAUDE.md) にまとめてある。

## 必要なもの

- OS : WSL2（Windows 側に VOICEPEAK がインストール済み）
- Python : 3.13 以上
- その他 : WSL interop が有効（`/etc/wsl.conf` の `[interop] enabled=true`, `appendWindowsPath=true`）

WSLg などで WSL 側に音声出力がある環境なら、再生だけを `paplay` / `aplay` / `ffplay`
に任せることもできる。

## セットアップ

### 1. 取得して動作確認する

```bash
git clone <this repo> ~/src/cc-hooks-voicepeak
cd ~/src/cc-hooks-voicepeak
./bin/cc-voicepeak check          # 環境診断
./bin/cc-voicepeak check --notes  # WSL 連携の注意点を表示
```

`pip install -e .`（または `uv pip install -e .`）でもインストールできる
（`cc-voicepeak` コマンドが使えるようになる）。  
インストールしなくても `bin/cc-voicepeak` をそのまま実行できる。

### 2. voicepeak.exe の場所を教える

以下は自動で探索するので、当てはまるなら設定は不要。

- `/mnt/{c,d,e}/Program Files/VOICEPEAK/voicepeak.exe`
- `/mnt/{c,d,e}/Program Files/AHS/VOICEPEAK/voicepeak.exe`
- 上記 2 つの `Program Files (x86)` 版
- `/mnt/c/Users/*/AppData/Local/Programs/VOICEPEAK/voicepeak.exe`
- `/mnt/c/Users/*/AppData/Local/VOICEPEAK/voicepeak.exe`

見つからない場合だけ明示する。

```bash
export CC_VOICEPEAK_EXE='/mnt/c/Program Files/VOICEPEAK/voicepeak.exe'
```

### 3. 声を選ぶ

```bash
./bin/cc-voicepeak narrators                # 使える声の一覧
./bin/cc-voicepeak emotions 'Miyamai Moca'  # その声の感情パラメータ
./bin/cc-voicepeak speak "接続テストです。" -n 'Miyamai Moca' -e 'honwaka=40'
```

気に入った設定は `.claude/voicepeak.json` に書いておく（`examples/voicepeak.json` 参照）。

### 4. Hook を登録する

登録の方法は 2 つある。プラグインとして入れるのが手軽で、`settings.json` を
自分で編集したい場合は従来どおりスニペットも使える。

#### A. プラグインとして入れる（手軽）

```
/plugin marketplace add aiwas/cc-hooks-voicepeak
/plugin install voicepeak@cc-hooks-voicepeak
```

これだけで `Stop` / `Notification` / `SubagentStop` の hook が登録され、
`/voicepeak:check` `/voicepeak:speak` `/voicepeak:split` が使えるようになる。
`settings.json` は触らない。

手元の clone をそのまま読ませることもできる。

```bash
claude --plugin-dir /path/to/cc-hooks-voicepeak
```

声や `voicepeak.exe` のパスは、プラグインで入れた場合も設定ファイル
（`.claude/voicepeak.json` など、後述の「設定」を参照）で指定する。

`SubagentStop` は hook としては登録されるが、既定では読み上げない。
サブエージェントの完了も読ませたい場合は設定の `hook.subagent` を `true` にする。

#### B. settings.json に手で登録する

```bash
./bin/cc-voicepeak install-hook                    # settings.json 用のスニペットを表示
./bin/cc-voicepeak install-hook --events Stop      # イベントを絞る
cc-voicepeak install-hook --installed              # PATH 上の cc-voicepeak を呼ぶ形で出力
cc-voicepeak install-hook --command '/opt/x/cc-voicepeak hook'   # コマンドを直接指定
```

既定では `$CLAUDE_PROJECT_DIR/bin/cc-voicepeak hook` を出力する。
`pip install` 経由でランチャ (`bin/cc-voicepeak`) が無い場合は、自動的に
`cc-voicepeak hook` の形に切り替わる。

スニペット本体は標準出力、案内文は標準エラーに出るので、`> snippet.json` でそのまま保存できる。  
表示された内容を `.claude/settings.json`（プロジェクト単位）か `~/.claude/settings.json`（全体）にマージする。
同じ内容を `examples/settings.json` にも置いてある（`Stop` でタスク完了を、`Notification` で
許可待ちを読み上げる形）。コピー元として使えるように、説明のコメントはファイルに入れていない。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/bin/cc-voicepeak hook",
            "timeout": 10
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/bin/cc-voicepeak hook",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

| イベント       | 読み上げる内容                                                        |
| -------------- | --------------------------------------------------------------------- |
| `Stop`         | タスク完了時。`transcript_path` から最後のアシスタント応答を拾う      |
| `Notification` | 許可待ち・入力待ちの通知                                              |
| `SubagentStop` | サブエージェントの完了。使う場合は設定の `hook.subagent` を `true` に |

- `timeout` は短くてよい。`cc-voicepeak hook` は読み上げ本体を別プロセスに渡して即終了するため、読み上げに何十秒かかっても会話はブロックされない。
- voicepeak が見つからないなどで失敗した場合も hook は終了コード 0 を返すので、読み上げの失敗で作業が止まることはない。

## 使い方

```
cc-voicepeak hook                            Claude Code Hooks から呼ばれる本体
cc-voicepeak speak "テキスト"                手動で読み上げ
cc-voicepeak speak -f notes.md -o out.wav    再生せず 1 本の wav に保存
cc-voicepeak split --stdin --json            分割結果だけ確認（合成しない）
cc-voicepeak check [--synth]                 環境診断（--synth で実際に 1 回合成）
cc-voicepeak check --notes                   WSL 連携の注意点
cc-voicepeak check --print-config            有効な設定を表示
cc-voicepeak narrators                       声の一覧
cc-voicepeak emotions <名前>                 感情パラメータの一覧
cc-voicepeak install-hook                    settings.json スニペット
```

プラグインとして入れた場合は、Claude Code から直接叩けるコマンドも使える。

| コマンド            | 内容                                             |
| ------------------- | ------------------------------------------------ |
| `/voicepeak:check`  | 環境診断。`--synth` を渡すと実際に 1 回合成する  |
| `/voicepeak:speak`  | 渡したテキストを読み上げる                       |
| `/voicepeak:split`  | 分割結果だけ確認する（合成しない）               |

共通オプション（サブコマンドの前に置く）:

| オプション                                   | 内容                         |
| -------------------------------------------- | ---------------------------- |
| `-v` / `--verbose`                           | ログを標準エラーにも出す     |
| `--log-level {debug,info,warning,error,off}` | ログレベル                   |
| `--config PATH`                              | 設定ファイルを追加で読み込む |

`speak` の主なオプション: `-n/--narrator` `-e/--emotion` `--speed` `--pitch` `--exe`
`--limit` `--min-fill` `--player` `--concat` `--no-normalize` `--no-cache` `--dry-run`
`-o/--out` `--stdin` `-f/--file` `--session` `--on-busy`。

`hook` には `--event`（イベント名の上書き）と `--sync`（別プロセスに投げず読み上げ完了
まで待つ。デバッグ用）がある。

長い文章がどこで区切られるかは、合成せずに確認できる。

```bash
$ ./bin/cc-voicepeak split -f notes.md
2 ブロック / 合計 210 文字 / 上限 140 文字
 [  1]   88 文字 | 実装が完了しました。まず設定ファイルを読み込む処理を追加し、次に分割のアルゴリズムを見直しています。…
 [  2]  122 文字 | voicepeak は一回の起動で百四十文字までしか受け付けないため、長い文章は必ず分割してから…
```

## 設定

設定ファイルは以下の順に読み込み、後のものが前を上書きする。

1. 組み込みデフォルト
2. `~/.config/cc-voicepeak/config.json`（`$XDG_CONFIG_HOME` 対応）
3. `.claude/voicepeak.json`（`$CLAUDE_PROJECT_DIR` が設定されていればそこ、
   無ければカレントディレクトリ）
4. `$CC_VOICEPEAK_CONFIG` が指すファイル
5. 環境変数（`CC_VOICEPEAK_EXE` `CC_VOICEPEAK_NARRATOR` `CC_VOICEPEAK_PLAYER` など）
6. `--config PATH` で指定したファイル
7. コマンドライン引数

`examples/voicepeak.json` が主要なキーを埋めた設定例で、2 か 3 の場所へそのまま置ける。
JSON にはコメントを書けず、`$comment` のような独自キーは `check` が
「既定値に無いキーです」と WARN を出すため、サンプルには説明を入れていない。

よく使うキーは以下。全項目は `cc-voicepeak check --print-config` で確認できる。

| キー                              | 既定                      | 説明                                                  |
| --------------------------------- | ------------------------- | ----------------------------------------------------- |
| `voicepeak.exe`                   | 自動探索                  | voicepeak.exe のパス                                  |
| `voicepeak.narrator`              | なし                      | 声（例 `Miyamai Moca`）                               |
| `voicepeak.emotion`               | なし                      | 感情（例 `happy=50,sad=0`）                           |
| `voicepeak.speed`                 | なし                      | 50 - 200                                              |
| `voicepeak.pitch`                 | なし                      | -300 ~ 300                                            |
| `voicepeak.char_limit`            | 140                       | 1ブロックの上限。140 より大きくはできない             |
| `voicepeak.cache`                 | true                      | 同じ文面・同じ声の wav を再利用する                   |
| `split.min_fill`                  | 0.55                      | ブロックをどれだけ詰めてから区切るか                  |
| `normalize.code_blocks`           | `placeholder`             | コードブロックの扱い。`drop` / `read` も選べる        |
| `normalize.max_total_chars`       | 0（無制限）               | 全体の文字数上限。長すぎる応答を切る                  |
| `normalize.replacements`          | 数件                      | 読み替え辞書 `[正規表現, 読み]`                       |
| `player.backend`                  | `auto`                    | `powershell` / `paplay` / `aplay` / `ffplay` / `none` |
| `player.concat`                   | false                     | 全ブロックを連結してから再生する                      |
| `hook.events`                     | `["Stop","Notification"]` | 反応するイベント                                      |
| `hook.on_busy`                    | `replace`                 | 読み上げ中に次が来たとき `replace` / `queue` / `skip` |
| `hook.min_chars`                  | 2                         | これより短い応答は読まない                            |
| `hook.prefix` / `.suffix`         | 空                        | 定型句（例 `"お疲れさまです。"`）                     |
| `log.level`                       | `info`                    | ログは `~/.local/state/cc-voicepeak/cc-voicepeak.log` |

## トラブルシューティング

| 症状                               | 対処                                                                                                    |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 何も起きない                       | `cc-voicepeak check` を実行し、ログ `~/.local/state/cc-voicepeak/cc-voicepeak.log` を確認する           |
| `voicepeak.exe を実行できません`   | interop が無効。`/etc/wsl.conf` を直して `wsl --shutdown`                                               |
| `wav が出力されませんでした`       | 出力先が Windows から書けない。`CC_VOICEPEAK_TEMP` を `/mnt/c/...` 配下に                               |
| 音が鳴らない（合成は成功している） | `powershell.exe` が PATH にない。`appendWindowsPath=true` を確認するか、`player.backend` を `paplay` に |
| 途中で不自然に切れる               | `split.min_fill` を下げる（0.3 など）。`cc-voicepeak split` で境界を確認                                |
| 長すぎて聞いていられない           | `normalize.max_total_chars` を 300 程度にする、または `hook.prefix` だけ読ませる運用にする              |
| 前の読み上げが終わらない           | `hook.on_busy` を `replace`（既定）にする。`skip` なら捨てる                                            |
| 141 文字エラーが出る               | `voicepeak.char_limit` を 130 程度に下げる（環境によって数え方が違う場合がある）                        |

## 開発

```bash
python3 -m unittest discover -s tests -t .                                    # テスト全件
uv run --no-project --python 3.13 python -m unittest discover -s tests -t .   # 下限の 3.13 で実行
```

Windows も VOICEPEAK も無い環境でも全件実行できる。

配布物を作る場合は `uv build`。`[build-system]` を見て setuptools を隔離環境へ
取得するので、環境に setuptools や pip を入れておく必要はない。

```bash
uv build          # dist/ に sdist と wheel
uv run --no-project --with dist/cc_hooks_voicepeak-*-py3-none-any.whl \
  cc-voicepeak --version
```

設計や内部構造は [CLAUDE.md](CLAUDE.md) を参照。

## ライセンス

MIT
