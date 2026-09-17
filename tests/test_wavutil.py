"""wav の連結と長さ計算のテスト."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from cc_voicepeak.wavutil import concat_wavs, wav_duration, write_silence


class WavTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-wav-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name: str, seconds: float = 0.2, framerate: int = 48000) -> Path:
        return write_silence(self.tmp / name, seconds=seconds, framerate=framerate)

    def make_stereo(self, name: str, seconds: float = 0.2) -> Path:
        path = self.tmp / name
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes(b"\x00\x00\x00\x00" * int(48000 * seconds))
        return path

    def frames_of(self, path: Path) -> int:
        with wave.open(str(path), "rb") as reader:
            return reader.getnframes()


class DurationTest(WavTestCase):
    def test_complete_wav(self):
        self.assertAlmostEqual(wav_duration(self.make("a.wav", seconds=1.0)), 1.0, places=3)

    def test_truncated_wav_is_not_overstated(self):
        full = self.make("full.wav", seconds=1.0)
        truncated = self.tmp / "trunc.wav"
        truncated.write_bytes(full.read_bytes()[: 44 + 4800])
        # ヘッダは 1.0 秒と主張するが、実際に読めるのは 0.05 秒ぶん
        self.assertAlmostEqual(wav_duration(truncated), 0.05, places=3)

    def test_missing_file(self):
        self.assertEqual(wav_duration(self.tmp / "nope.wav"), 0.0)

    def test_broken_header(self):
        path = self.tmp / "broken.wav"
        path.write_bytes(b"RIFF" + b"\x00" * 100)
        self.assertEqual(wav_duration(path), 0.0)

    def test_header_cut_short(self):
        # wave が EOFError を投げる経路
        full = self.make("full.wav")
        path = self.tmp / "short.wav"
        path.write_bytes(full.read_bytes()[:30])
        self.assertEqual(wav_duration(path), 0.0)


class ConcatTest(WavTestCase):
    def test_sources_are_joined(self):
        first = self.make("a.wav", seconds=0.2)
        second = self.make("b.wav", seconds=0.3)
        dest = concat_wavs([first, second], self.tmp / "out.wav")
        self.assertIsNotNone(dest)
        self.assertAlmostEqual(wav_duration(dest), 0.5, places=3)

    def test_empty_list(self):
        self.assertIsNone(concat_wavs([], self.tmp / "out.wav"))
        self.assertFalse((self.tmp / "out.wav").exists())

    def test_only_unusable_sources(self):
        missing = self.tmp / "nope.wav"
        tiny = self.tmp / "tiny.wav"
        tiny.write_bytes(b"RIFF")
        self.assertIsNone(concat_wavs([missing, tiny], self.tmp / "out.wav"))
        self.assertFalse((self.tmp / "out.wav").exists())

    def test_gap_is_inserted_between_sources(self):
        first = self.make("a.wav", seconds=0.2)
        second = self.make("b.wav", seconds=0.2)
        dest = concat_wavs([first, second], self.tmp / "out.wav", gap_seconds=0.1)
        # 末尾には入れないので 0.2 + 0.1 + 0.2
        self.assertAlmostEqual(wav_duration(dest), 0.5, places=3)

    def test_gap_keeps_frame_alignment(self):
        first = self.make("a.wav", seconds=0.2)
        second = self.make("b.wav", seconds=0.2)
        dest = concat_wavs([first, second], self.tmp / "out.wav", gap_seconds=0.037)
        with wave.open(str(dest), "rb") as reader:
            data = reader.readframes(reader.getnframes())
        self.assertEqual(len(data) % (2 * 1), 0, "フレーム境界がずれている")

    def test_same_path_twice_still_gets_a_gap(self):
        # 同一オブジェクトの同一性比較だと末尾判定を誤る
        path = self.make("a.wav", seconds=0.2)
        dest = concat_wavs([path, path], self.tmp / "out.wav", gap_seconds=0.1)
        self.assertAlmostEqual(wav_duration(dest), 0.5, places=3)

    def test_mismatched_format_is_skipped(self):
        mono = self.make("a.wav", seconds=0.2)
        stereo = self.make_stereo("b.wav", seconds=0.2)
        dest = concat_wavs([mono, stereo], self.tmp / "out.wav")
        self.assertIsNotNone(dest, "1 件でも読めれば dest を返す")
        self.assertAlmostEqual(wav_duration(dest), 0.2, places=3)

    def test_unreadable_source_is_skipped(self):
        good = self.make("a.wav", seconds=0.2)
        broken = self.tmp / "broken.wav"
        broken.write_bytes(b"RIFF" + b"\x00" * 200)
        dest = concat_wavs([broken, good], self.tmp / "out.wav")
        self.assertIsNotNone(dest)
        self.assertAlmostEqual(wav_duration(dest), 0.2, places=3)

    def test_truncated_source_does_not_abort(self):
        good = self.make("a.wav", seconds=0.2)
        full = self.make("full.wav", seconds=1.0)
        truncated = self.tmp / "trunc.wav"
        truncated.write_bytes(full.read_bytes()[:30])
        dest = concat_wavs([truncated, good], self.tmp / "out.wav")
        self.assertIsNotNone(dest)

    def test_no_partial_file_is_left_behind(self):
        broken = self.tmp / "broken.wav"
        broken.write_bytes(b"RIFF" + b"\x00" * 200)
        out = self.tmp / "out.wav"
        self.assertIsNone(concat_wavs([broken], out))
        self.assertFalse(out.exists(), "中途半端な dest が残っている")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [], "一時ファイルが残っている")

    def test_existing_dest_is_kept_when_nothing_is_usable(self):
        out = self.tmp / "out.wav"
        previous = self.make("prev.wav", seconds=0.4)
        shutil.copyfile(previous, out)
        broken = self.tmp / "broken.wav"
        broken.write_bytes(b"RIFF" + b"\x00" * 200)
        self.assertIsNone(concat_wavs([broken], out))
        self.assertAlmostEqual(wav_duration(out), 0.4, places=3)


class WriteSilenceTest(WavTestCase):
    def test_duration_and_format(self):
        path = write_silence(self.tmp / "s.wav", seconds=0.25, framerate=16000)
        with wave.open(str(path), "rb") as reader:
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), 16000)
        self.assertAlmostEqual(wav_duration(path), 0.25, places=3)

    def test_parent_directory_is_created(self):
        path = write_silence(self.tmp / "deep" / "s.wav")
        self.assertTrue(path.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
