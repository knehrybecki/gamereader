"""Skracanie kwestii lektora z sesji GTA VI (Chrome, 09.10): bez modeli, dźwięku i ekranu."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine


class FakeTranslator:
    def __init__(self, out):
        self.out = out
        self.seen = []

    def translate(self, text):
        self.seen.append(text)
        return self.out


class FakeLektor:
    cps1 = 15.0

    def plan(self, text):
        return [(text, "calm")]

    def overload(self, text, seconds):
        return len(text or "") / (self.cps1 * max(0.6, seconds * 0.92)) / 1.25

    def line_boost(self, text, seconds):
        return 1.0


def make_engine(translator, seconds):
    engine = Engine.__new__(Engine)
    engine.translator = translator
    engine.lektor = FakeLektor()
    engine.prosody = Mock(recent=Mock(return_value=0.0))
    engine._heard_arousal = {}
    engine.catch_up = False
    engine._screen_budget = Mock(return_value=seconds)
    engine._timing = Mock()
    return engine


class LektorShorteningTest(unittest.TestCase):
    def test_mm_hmm_never_reaches_the_translator(self):
        # Argos zrobił z „Mm-hmm.” czytane na głos „Min-hamm.” (5 razy w jednej sesji)
        translator = FakeTranslator("Chodź. Jesteś na miejscu?")
        engine = make_engine(translator, 4.0)
        engine._plan_line("Come on. Mm-hmm. You in?", True)
        self.assertEqual(len(translator.seen), 1)
        self.assertNotIn("hmm", translator.seen[0].lower())

    def test_only_a_grunt_is_not_translated_at_all(self):
        translator = FakeTranslator("Min-hamm.")
        engine = make_engine(translator, 2.0)
        text, segments, _arousal, _boost = engine._plan_line("Mm-hmm.", True)
        self.assertEqual(translator.seen, [])
        self.assertEqual((text, segments), ("", []))

    def test_overloaded_single_line_drops_interjections_first(self):
        translator = FakeTranslator("Hej. Tak. Musimy natychmiast ruszyć do magazynu przy porcie.")
        engine = make_engine(translator, 2.2)
        text, _segments, _arousal, _boost = engine._plan_line("Hey. Yeah. We gotta move to the warehouse.", True)
        self.assertEqual(text, "Musimy natychmiast ruszyć do magazynu przy porcie.")

    def test_line_that_fits_is_read_in_full(self):
        translator = FakeTranslator("Hej. Tak. Idziemy.")
        engine = make_engine(translator, 6.0)
        text, _segments, _arousal, _boost = engine._plan_line("Hey. Yeah. We go.", True)
        self.assertEqual(text, "Hej. Tak. Idziemy.")


if __name__ == "__main__":
    unittest.main()
