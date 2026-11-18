"""Menu „Jakość głosu”: Szybka = 6 kroków syntezy VoiceStudio, Wysoka = 8 (prośba użytkownika 18.11)."""
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


class VoiceQualityTest(unittest.TestCase):
    def _engine(self):
        engine = ge.Engine.__new__(ge.Engine)
        cloud = ge.OmniVoiceLocal.__new__(ge.OmniVoiceLocal)
        cloud.steps = ge.OMNI_STEPS
        engine.lektor = SimpleNamespace(cloud=cloud)
        engine.voice_quality = "fast"
        return engine, cloud

    def test_high_quality_uses_eight_steps(self):
        engine, cloud = self._engine()
        engine.set_voice_quality("high")
        self.assertEqual(cloud.steps, 8)
        self.assertEqual(engine.voice_quality, "high")

    def test_fast_quality_uses_six_steps(self):
        engine, cloud = self._engine()
        engine.set_voice_quality("high")
        engine.set_voice_quality("fast")
        self.assertEqual(cloud.steps, 6)

    def test_unknown_value_keeps_current(self):
        engine, cloud = self._engine()
        engine.set_voice_quality("ultra")
        self.assertEqual((engine.voice_quality, cloud.steps), ("fast", 6))


if __name__ == "__main__":
    unittest.main()
