"""例外定義."""


class CcVoicepeakError(Exception):
    """このパッケージ由来のエラーの基底クラス."""


class ConfigError(CcVoicepeakError):
    """設定ファイル/設定値の不備."""


class BridgeError(CcVoicepeakError):
    """WSL <-> Windows 連携まわりの失敗."""


class SynthError(CcVoicepeakError):
    """voicepeak.exe の実行失敗."""


class LockTimeout(SynthError):
    """EXE の直列化ロックを取得できなかった.

    voicepeak は同時起動できないため、ロックを取れないまま合成に進んではいけない。
    再試行しても待ち時間が伸びるだけなので、合成失敗として扱う。
    """


class PlayerError(CcVoicepeakError):
    """再生の失敗."""


class RuntimeDirError(CcVoicepeakError):
    """ランタイムディレクトリを自分専用の状態で用意できなかった.

    状態ファイルとロックの置き場所が他ユーザから書ける状態だと、割り込みで
    無関係なプロセスを止めたり、ロックを握られて合成が止まったりする。
    黙って続行せず、読み上げ自体を諦める。
    """
