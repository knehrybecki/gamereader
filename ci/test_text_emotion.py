"""24.11 użytkownik: „ja bym wolał emocje opierać z tekstu, a nie z dźwięku”. Przejęcie lektora (-1 szept … +1 krzyk)
liczy się z polskiego tekstu kwestii (wykrzyknik, wielokropek, słowa żywe i miękkie), nie z głosu postaci w dźwięku
gry. Lektor VoiceStudio czyta równo: po fragmencie stała krótka pauza (dawniej 0,05–0,16 s zależnie od emocji)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


class TextArousalTest(unittest.TestCase):
    def test_plain_line_is_neutral(self):
        self.assertEqual(ge.text_arousal("Dobra, jedziemy do portu."), 0.0)
        self.assertEqual(ge.text_arousal(""), 0.0)

    def test_exclamation_and_lively_words_raise_it(self):
        self.assertGreater(ge.text_arousal("Uciekaj!"), 0.0)
        self.assertGreater(ge.text_arousal("Gliny jadą, szybko."), 0.0)
        self.assertGreater(ge.text_arousal("Co ty robisz?!"), ge.text_arousal("Co ty robisz!"))

    def test_trailing_off_and_soft_words_lower_it(self):
        self.assertLess(ge.text_arousal("Nie wiem…"), 0.0)
        self.assertLess(ge.text_arousal("Przepraszam, nie chciałem."), 0.0)

    def test_stays_in_range(self):
        for text in ("SZYBKO!!! UCIEKAJ!!! GLINY!!!", "Przepraszam… kocham cię… żegnaj…"):
            self.assertLessEqual(abs(ge.text_arousal(text)), 1.0)


class FakeLektor:
    cps1 = 15.0

    def plan(self, text):
        return [(text, "calm")]

    def overload(self, text, seconds):
        return 0.0

    def line_boost(self, text, seconds, lag=0.0):
        return 1.0


def make_engine(translated=None):
    engine = ge.Engine.__new__(ge.Engine)
    engine.translator = Mock(translate=Mock(return_value=translated))
    engine.lektor = FakeLektor()
    # dźwięk gry mówi coś innego niż tekst — ma zostać pominięty
    engine.prosody = Mock(recent=Mock(return_value=-0.9), analyze=Mock(return_value=-0.9))
    engine.catch_up = False
    engine._screen_budget = Mock(return_value=6.0)
    engine._timing = Mock()
    engine.brain = None
    return engine


class PlanLineEmotionTest(unittest.TestCase):
    def test_polish_subtitle_emotion_comes_from_text(self):
        engine = make_engine()
        _text, _segments, arousal, _boost = engine._plan_line("Uciekaj!", False)
        self.assertEqual(arousal, ge.text_arousal("Uciekaj!"))
        engine.prosody.recent.assert_not_called()

    def test_translated_line_emotion_comes_from_polish_text(self):
        engine = make_engine("Uciekaj!")
        text, _segments, arousal, _boost = engine._plan_line("Run!", True)
        self.assertEqual(text, "Uciekaj!")
        self.assertEqual(arousal, ge.text_arousal("Uciekaj!"))


class VoiceStudioPauseTest(unittest.TestCase):
    def prepare(self, text, arousal):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lektor = ge.MaleLektor.__new__(ge.MaleLektor)
        lektor.ensure = lambda: None
        lektor.cloud = Mock(failed_until=0.0, tag="ov|lektor", voice_rev={})
        lektor.tts_scale = 1.0
        lektor.ffmpeg = None
        lektor.voice = "M1"
        got = {}

        def synth_cloud(cloud, text, raw, pace, pause, voice, style):
            got["pause"] = pause
            return 1.0

        lektor._synth_cloud = synth_cloud
        lektor._master = lambda *args: None
        with patch.object(ge, "CACHE_DIR", Path(tmp.name)):
            lektor.prepare(text, arousal=arousal)
        return got["pause"]

    def test_pause_does_not_depend_on_emotion(self):
        pauses = {self.prepare("Dobra, jedziemy.", a) for a in (-1.0, 0.0, 1.0)}
        self.assertEqual(pauses, {ge.LEKTOR_CLOUD_PAUSE})

    def test_punctuation_pause_still_applies(self):
        self.assertGreater(self.prepare("Nie wiem…", 0.0), ge.LEKTOR_CLOUD_PAUSE)


if __name__ == "__main__":
    unittest.main()
