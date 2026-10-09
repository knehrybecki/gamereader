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

    def test_sentence_just_spoken_is_not_read_again_in_the_next_line(self):
        # sesja 10:11: „W ósmej klasie. To wszystko przez to gówno.” poszło w dwóch kolejnych kwestiach
        engine = make_engine(FakeTranslator("W ósmej klasie. To wszystko przez to gówno. To jest Sam."), 8.0)
        engine._said_sentences = {}
        engine._remember_sentences("W ósmej klasie. To wszystko przez to gówno.")
        text, _segments, _arousal, _boost = engine._plan_line("Eighth grade. Shit was all stems. This is Sam.", True)
        self.assertEqual(text, "To jest Sam.")

    def test_short_exclamations_may_repeat(self):
        engine = make_engine(FakeTranslator("Nie. Nie. Uciekaj stąd natychmiast."), 8.0)
        engine._said_sentences = {}
        engine._remember_sentences("Nie. Nie. Uciekaj stąd natychmiast.")
        text, _segments, _arousal, _boost = engine._plan_line("No. No. Get out of here right now.", True)
        self.assertIn("Nie.", text)

    def test_never_drops_the_whole_line_as_a_repeat(self):
        engine = make_engine(FakeTranslator("To wszystko przez to gówno."), 8.0)
        engine._said_sentences = {}
        engine._remember_sentences("To wszystko przez to gówno.")
        text, _segments, _arousal, _boost = engine._plan_line("Shit was all stems.", True)
        self.assertEqual(text, "To wszystko przez to gówno.")

    def test_late_lektor_condenses_a_line_that_would_still_fit(self):
        # spóźnienie ≥ 2 s: wtrącenie („No wiesz,”) wypada, mimo że kwestia mieści się w czasie napisu
        engine = make_engine(FakeTranslator("No wiesz, musimy ruszyć do magazynu."), 8.0)
        engine._lag = 2.4
        text, _segments, _arousal, _boost = engine._plan_line("Well, we gotta move to the warehouse.", True)
        self.assertEqual(text, "Musimy ruszyć do magazynu.")

    def test_on_time_lektor_keeps_the_line_intact(self):
        engine = make_engine(FakeTranslator("No wiesz, musimy ruszyć do magazynu."), 8.0)
        engine._lag = 0.4
        text, _segments, _arousal, _boost = engine._plan_line("Well, we gotta move to the warehouse.", True)
        self.assertEqual(text, "No wiesz, musimy ruszyć do magazynu.")

    def test_very_late_lektor_drops_interjection_sentences_even_when_they_fit(self):
        engine = make_engine(FakeTranslator("Tak. Musimy ruszyć do magazynu."), 8.0)
        engine._lag = 4.0
        text, _segments, _arousal, _boost = engine._plan_line("Yeah. We gotta move to the warehouse.", True)
        self.assertEqual(text, "Musimy ruszyć do magazynu.")

    def test_translated_line_does_not_wait_for_the_characters_voice(self):
        engine = make_engine(FakeTranslator(""), 4.0)
        engine.voice = Mock()
        engine.voice.alive.side_effect = AssertionError("tłumaczenie nie pyta o głos postaci")
        self.assertEqual(engine._film_entry("Hello there.", 10.0, True), 0.0)


if __name__ == "__main__":
    unittest.main()
