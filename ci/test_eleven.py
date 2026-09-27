"""ElevenLabs jako głos lektora — bez sieci (atrapa API)."""
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


def pcm(seconds=1.0):
    t = np.arange(int(ge.ELEVEN_RATE * seconds)) / ge.ELEVEN_RATE
    return (np.sin(2 * np.pi * 180 * t) * 8000).astype("<i2").tobytes()


class ElevenLabsTest(unittest.TestCase):
    def test_request_carries_polish_film_lektor_settings(self):
        tts = ge.ElevenLabsTTS("key", "voice-1!")
        self.assertEqual(tts.voice, "voice1")
        with patch.object(tts, "_request", return_value=(200, pcm())) as req:
            audio = tts.synth("Złap Franklina.", speed=1.5, previous="Tato!")
        method, path, body = req.call_args[0]
        self.assertEqual((method, body["language_code"], body["model_id"]), ("POST", "pl", ge.ELEVEN_MODEL))
        self.assertIn("/v1/text-to-speech/voice1", path)
        self.assertLessEqual(body["voice_settings"]["speed"], 1.2)
        self.assertEqual(body["previous_text"], "Tato!")
        self.assertGreater(audio.size, 1000)

    def test_bad_key_backs_off_to_supertonic(self):
        tts = ge.ElevenLabsTTS("bad", "v")
        with patch.object(tts, "_request", return_value=(401, b'{"detail": {"message": "Invalid API key"}}')):
            self.assertIsNone(tts.synth("Hej."))
        self.assertIn("Invalid API key", tts.error)
        with patch.object(tts, "_request") as req:
            self.assertIsNone(tts.synth("Hej."))  # nie pyta co kwestię
            req.assert_not_called()

    def test_lektor_writes_cloud_voice_and_falls_back(self):
        lektor = ge.MaleLektor()
        lektor.ffmpeg = None
        lektor.set_cloud("key", "v")
        errors = []
        lektor.on_cloud_error = errors.append
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a.wav"
            with patch.object(lektor.cloud, "_request", return_value=(200, pcm())):
                self.assertEqual(lektor._synth_cloud(lektor.cloud, "Złap Franklina.", out, 1.0, 0.1), 1.0)
            with wave.open(str(out)) as handle:
                self.assertEqual(handle.getframerate(), ge.ELEVEN_RATE)
            with patch.object(lektor.cloud, "_request", side_effect=OSError("offline")):
                self.assertIsNone(lektor._synth_cloud(lektor.cloud, "Hej.", out, 1.0, 0.1))
        self.assertTrue(errors and "Supertonic" in errors[-1])


if __name__ == "__main__":
    unittest.main()
