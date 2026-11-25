"""Spóźniony lektor VoiceStudio przyspiesza do sufitu ×1,50 (18.11 zasada ×1,15; 21.11 ×1,20; 25.11 użytkownik:
„lektor jest za wolny względem tekstu, przyspiesz” — pomiar lektor-hd: do ×1,40 zero zgubionych słów; tego samego wieczoru „wolę szybciej… przyspieszyć jeszcze trochę” → ×1,50).

18.11: sufit nadganiania liczony pod Supertonic (LEKTOR_MAX_RATE / (LEKTOR_SPEED / tts)) przy profilu gry tts 0,92
wynosił ×1,04 — lektor 3,6 s za napisem czytał „tempo x1.05” i pomijał zaległe kwestie."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


def lektor(cloud_ready):
    lk = ge.MaleLektor.__new__(ge.MaleLektor)
    lk.tts_scale = 0.92  # profil gry (GTA / YouTube)
    lk.cps1 = ge.LEKTOR_CPS_PRIOR
    lk.cloud = SimpleNamespace(ready=True, failed_until=0.0) if cloud_ready else None
    return lk


class CatchUpTest(unittest.TestCase):
    def test_late_voicestudio_lektor_speeds_up_to_150(self):
        self.assertEqual(lektor(True).line_boost("Chcę to zrobić po cichu.", 3.0, lag=3.0), ge.LEKTOR_CLOUD_MAX_BOOST)
        self.assertEqual(ge.LEKTOR_CLOUD_MAX_BOOST, 1.50)

    def test_on_time_voicestudio_lektor_reads_at_base_tempo(self):
        self.assertEqual(ge.LEKTOR_CLOUD_BASE_BOOST, 1.20)
        self.assertEqual(lektor(True).line_boost("Chcę to zrobić po cichu.", 3.0, lag=0.0), 1.20)

    def test_supertonic_on_time_keeps_normal_pace(self):
        self.assertEqual(lektor(False).line_boost("Chcę to zrobić po cichu.", 3.0, lag=0.0), 1.0)

    def test_supertonic_cap_unchanged(self):
        # Supertonic bełkocze powyżej LEKTOR_MAX_RATE — jego sufit zostaje jak był
        self.assertEqual(lektor(False).line_boost("Chcę to zrobić po cichu.", 3.0, lag=3.0), 1.05)

    def test_voicestudio_gets_full_catch_up_tempo(self):
        # pace 0,6 (tts 0,92 / boost 1,50) → sufit nadganiania ×1,50; limity
        # Supertonic (LEKTOR_MAX_RATE) ucinały do ~1,13
        lk = lektor(True)
        lk._cloud_prev = ("", 0.0)
        lk.on_cloud_error = None
        lk._write_voice = lambda *a: None
        got = {}

        class Cloud:
            rate = 24000
            error = ""

            def synth(self, text, speed=1.0, **kwargs):
                got["speed"] = speed
                return ge.np.zeros(2400, dtype=ge.np.float32)

        lk._synth_cloud(Cloud(), "Chcę to zrobić po cichu.", "out.wav", 0.6, 0.0)
        self.assertAlmostEqual(got["speed"], ge.LEKTOR_CLOUD_MAX_BOOST, places=2)


if __name__ == "__main__":
    unittest.main()
