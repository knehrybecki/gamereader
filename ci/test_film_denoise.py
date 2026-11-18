"""Głos z filmu (voice.json „denoise”: true): resztki tła w pauzach i szum przed pierwszym słowem
(„cichy pisk na początku”, 18.11) wycinane w gotowym głosie lektora; próbka i tempo bez zmian."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


class FilmDenoiseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.packs = Path(self.tmp.name)
        (self.packs / "film").mkdir()
        (self.packs / "film/voice.wav").write_bytes(b"RIFF")
        self.p = patch.object(ge, "VOICEPACK_DIR", self.packs)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def _pack(self, meta):
        (self.packs / "film/voice.json").write_text(json.dumps(meta))
        return ge.OmniVoiceLocal("film")

    def test_film_pack_cleans_output_and_gets_new_recordings(self):
        plain = self._pack({"lps": 10.18})
        film = self._pack({"lps": 10.18, "denoise": True})
        self.assertNotIn("afftdn", plain.pre_filter)
        self.assertIn("afftdn", film.pre_filter)
        self.assertNotEqual(plain.tag, film.tag)

    @unittest.skipUnless(shutil.which("ffmpeg"), "brak ffmpeg")
    def test_noise_before_first_word_is_cut(self):
        sr = 24000
        rng = np.random.default_rng(0)
        lead = 0.0045 * rng.standard_normal(int(sr * 0.6))  # szum ~27 dB pod głosem, jak w nagraniu z filmu
        t = np.arange(int(sr * 1.0)) / sr
        voice = 0.15 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
        audio = np.concatenate([lead, voice]).astype(np.float32)
        film = self._pack({"denoise": True})
        src, out = Path(self.tmp.name) / "in.raw", Path(self.tmp.name) / "out.raw"
        src.write_bytes(audio.tobytes())
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", str(src),
                        "-af", film.pre_filter, "-f", "f32le", str(out)], check=True)
        got = np.frombuffer(out.read_bytes(), dtype=np.float32)
        lead_left = (len(got) - len(voice)) / sr
        self.assertLess(lead_left, 0.15)  # z 0,6 s szumu zostaje najwyżej zapas przed słowem
        self.assertGreater(len(got), len(voice) * 0.95)  # samo słowo nietknięte


if __name__ == "__main__":
    unittest.main()
