"""Lektor VoiceStudio równy jak w filmie (18.11: „ucina zdanie na końcu albo są artefakty i są zbyt duże różnice
głosu zależnie od emocji — musisz to wyrównać”): jedna próbka, tempo i głośność niezależne od emocji, sufit tempa."""
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


class EvenLektorTest(unittest.TestCase):
    def setUp(self):
        lektor = ge.MaleLektor.__new__(ge.MaleLektor)
        lektor.ensure = lambda: None
        lektor.voice = "M5"
        lektor.tts_scale = 0.92  # profil gry
        lektor.ffmpeg = None
        lektor.cloud = SimpleNamespace(tag="ov1|pack", failed_until=0.0, voice_rev={}, voice_session="s")
        lektor._synth_cloud = Mock(return_value=1.0)
        self.gains = []

        def master(raw, path, gain, *_):
            self.gains.append(gain)
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(24000)
                handle.writeframes(b"\0" * 2400)

        lektor._master = master
        self.lektor = lektor
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(ge, "CACHE_DIR", Path(self.tmp.name)), patch.object(ge, "log_timing", lambda _: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_emotion_does_not_change_voice_tempo_or_loudness(self):
        self.lektor.prepare("Uciekaj stąd, szybko", mood="calm", arousal=0.0, boost=1.0)
        self.lektor.prepare("Uciekaj stąd, szybko", mood="tense", arousal=1.0, boost=1.0)
        (calm, excited) = [c.args for c in self.lektor._synth_cloud.call_args_list]
        self.assertAlmostEqual(calm[3], excited[3])  # pace
        self.assertEqual(self.gains[0], self.gains[1])
        self.assertIsNone(calm[6])
        self.assertIsNone(excited[6])  # bez drugiej („żywej”) próbki — inna barwa w środku sceny

    def test_rate_never_above_catch_up_ceiling(self):
        got = {}

        class Cloud:
            rate = 24000
            error = ""

            def synth(self, text, speed=1.0, **kwargs):
                got["speed"] = speed
                return ge.np.zeros(2400, dtype=ge.np.float32)

        lk = ge.MaleLektor.__new__(ge.MaleLektor)
        lk._cloud_prev = ("", 0.0)
        lk.on_cloud_error = None
        lk._write_voice = lambda *a: None
        lk._synth_cloud(Cloud(), "Piątek rano, napad na bank. Błagam.", "out.wav", 0.5, 0.0)
        self.assertLessEqual(got["speed"], ge.LEKTOR_CLOUD_MAX_BOOST)  # 18.11: ×1,6 gubiło „Błagam.”

    def test_exclamation_and_mood_do_not_change_loudness(self):
        self.lektor.prepare("Uciekaj stąd!", mood="tense", arousal=0.0, boost=1.0)
        self.lektor.prepare("Uciekaj stąd.", mood="calm", arousal=0.0, boost=1.0)
        self.assertEqual(self.gains[0], self.gains[1])

    def _sent_to_model(self, text, voice=None):
        sent = []

        class Cloud:
            rate = 24000
            error = ""

            def synth(self, text, **kwargs):
                sent.append(text)
                return ge.np.zeros(2400, dtype=ge.np.float32)

        lk = ge.MaleLektor.__new__(ge.MaleLektor)
        lk._cloud_prev = ("", 0.0)
        lk.on_cloud_error = None
        lk._write_voice = lambda *a: None
        lk._synth_cloud(Cloud(), text, "out.wav", 1.0, 0.0, voice)
        return sent[0]

    def test_lektor_reads_exclamations_flat(self):
        # z „!” OmniVoice zaczyna krzyczeć — lektor filmowy czyta równo; pytanie zostaje pytaniem
        self.assertEqual(self._sent_to_model("Uciekaj stąd! Szybko!!"), "Uciekaj stąd. Szybko.")
        self.assertEqual(self._sent_to_model("Co?! Serio?"), "Co? Serio?")
        self.assertEqual(self._sent_to_model("Uciekaj stąd!", voice="bohater_jason"), "Uciekaj stąd!")

    def test_eight_steps_by_default(self):
        self.assertEqual(ge.OMNI_QUALITY_STEPS[ge.VOICE_QUALITY_DEFAULT], 8)


if __name__ == "__main__":
    unittest.main()
