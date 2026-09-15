---
description: VOICEPEAK 読み上げの環境診断。WSL interop、voicepeak.exe の場所、再生経路、設定ファイルをまとめて確認する。
argument-hint: "[--synth]"
---

# VOICEPEAK 読み上げの環境診断

次を実行して、結果を日本語で要約する。

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cc-voicepeak" check $ARGUMENTS
```

`$ARGUMENTS` に `--synth` が含まれていれば、実際に 1 ブロックだけ合成して再生まで通す。
何も指定が無ければ実行だけで音は鳴らさない。

## 出力の読み方

各行は `[ OK ]` / `[WARN]` / `[FAIL]` のいずれか。`→` で始まる行が対処方法。

- **`[FAIL]` があるとき** — 読み上げは動かない。最初の FAIL から順に対処する。
  よくあるのは `voicepeak` の FAIL（`voicepeak.exe` が見つからない）で、
  設定 `voicepeak.exe` か環境変数 `CC_VOICEPEAK_EXE` にフルパスを与えれば解決する。
- **`[WARN]` だけのとき** — 読み上げ自体は動く。内容によっては意図どおりでない
  可能性がある、という通知。
  - `設定キー: <名前>` — 既定値に無いキー。設定ファイルの綴り誤りを疑う
  - `player.volume` 関連 — 常駐 PowerShell の `SoundPlayer` には音量 API が無いため、
    音量指定は反映されない。変えたい場合は `player.backend` を `paplay` / `ffplay` にする

## 補足

WSL 連携そのものでつまずいている場合は、続けて注意点の一覧も出せる。

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cc-voicepeak" check --notes
```

現在の設定値をすべて確認したい場合はこちら。

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cc-voicepeak" check --print-config
```
