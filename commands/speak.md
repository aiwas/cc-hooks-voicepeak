---
description: 任意のテキストを VOICEPEAK に読み上げさせる。声や感情の指定、wav への保存もできる。
argument-hint: "[読み上げたいテキスト]"
---

# VOICEPEAK に読み上げさせる

`$ARGUMENTS` を読み上げる。

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cc-voicepeak" speak "$ARGUMENTS"
```

`$ARGUMENTS` が空のときは、直前の会話の内容を読み上げてほしいのか、
それとも読み上げるテキストを指定したいのかをユーザーに確認する。勝手に決めない。

## オプションの並び順に注意

`--config` / `-v` / `--log-level` は `cc-voicepeak` 直下のオプションなので
**サブコマンドより前**、声の指定は `speak` のオプションなので **後ろ**に置く。
順序を間違えると argparse が `unrecognized arguments` で終了する。

```bash
# 正しい
"${CLAUDE_PLUGIN_ROOT}/bin/cc-voicepeak" -v speak "テキスト" -n 'Miyamai Moca'

# 間違い（-v が後ろ）
"${CLAUDE_PLUGIN_ROOT}/bin/cc-voicepeak" speak "テキスト" -v
```

## よく使う指定

| やりたいこと | 追加する引数 |
|---|---|
| 声を変える | `-n 'Miyamai Moca'`（一覧は `cc-voicepeak narrators`） |
| 感情を付ける | `-e 'honwaka=40'`（一覧は `cc-voicepeak emotions '<声の名前>'`） |
| ファイルを読ませる | `-f notes.md`（`$ARGUMENTS` の代わり） |
| 再生せず wav に保存 | `-o out.wav` |
| 合成せず分割結果だけ見る | `--dry-run` |
| 継ぎ目を消す | `--concat`（全ブロックを 1 本に連結してから再生） |
| 再生方法を変える | `--player paplay`（`powershell` / `paplay` / `aplay` / `ffplay` / `none`） |

## 失敗したとき

終了コードが 0 以外なら、`-v` を付けて実行し直してログを見る。
`voicepeak.exe` が見つからない場合は `/voicepeak:check` で環境を確認する。
