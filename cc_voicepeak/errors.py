"""例外定義."""


class CcVoicepeakError(Exception):
    """このパッケージ由来のエラーの基底クラス."""


class ConfigError(CcVoicepeakError):
    """設定ファイル/設定値の不備."""


class BridgeError(CcVoicepeakError):
    """WSL <-> Windows 連携まわりの失敗."""


class SynthError(CcVoicepeakError):
    """voicepeak.exe の実行失敗."""


class PlayerError(CcVoicepeakError):
    """再生の失敗."""
