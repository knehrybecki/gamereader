"""Skracanie kwestii lektora z sesji GTA VI (Chrome, 09.10): bez modeli, dźwięku i ekranu."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from gamereader_engine import (
    AppleVisionOcr, Engine, MaleLektor, LEKTOR_MAX_RATE, LEKTOR_SPEED, drop_dim_lines, ink_brightness,
)


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

    def line_boost(self, text, seconds, lag=0.0):
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

    def _real_lektor(self):
        lektor = MaleLektor.__new__(MaleLektor)
        lektor.cps1 = 15.0
        lektor.tts_scale = 1.0
        return lektor

    def test_late_lektor_speeds_up_even_when_the_subtitle_budget_is_loose(self):
        # sesja 10:22: ×1,00 przy 30 pominiętych kwestiach, bo budżet z tempa napisów był za luźny
        lektor = self._real_lektor()
        text = "Powinieneś zobaczyć tego gościa."
        self.assertEqual(lektor.line_boost(text, 6.0, 0.0), 1.0)
        self.assertEqual(lektor.line_boost(text, 6.0, 1.0), 1.0)
        mid = lektor.line_boost(text, 6.0, 1.75)
        top = lektor.line_boost(text, 6.0, 2.5)
        self.assertGreater(mid, 1.0)
        self.assertGreater(top, mid)

    def test_lag_never_pushes_the_tempo_past_the_ceiling(self):
        lektor = self._real_lektor()
        boost = lektor.line_boost("Krótka kwestia.", 6.0, 30.0)
        self.assertLessEqual(boost * LEKTOR_SPEED, LEKTOR_MAX_RATE + 0.03)


class DimLinesTest(unittest.TestCase):
    """Jasność liter z kadrów GTA VI (09.10): jasna linia = mówiąca postać, ciemniejsza = osoba poboczna."""

    def test_same_colour_lines_are_both_read(self):
        # zmierzone: 229/231, 234/238, 235/236 — dwie linie jednego koloru
        for first, second in ((229, 231), (234, 238), (235, 236)):
            kept = drop_dim_lines([("Jedna.", first, 5000), ("Druga.", second, 3000)])
            self.assertEqual(kept, ["Jedna.", "Druga."])

    def test_clearly_darker_line_is_dropped(self):
        # zmierzone: „Hey, Billy!” 214, „Yo, what’s up?” 188
        kept = drop_dim_lines([("Hey, Billy!", 214, 1080), ("Yo, what's up?", 188, 860)])
        self.assertEqual(kept, ["Hey, Billy!"])

    def test_darker_first_line_is_dropped_too(self):
        kept = drop_dim_lines([("Dodaje.", 185, 900), ("Mówi.", 230, 1200)])
        self.assertEqual(kept, ["Mówi."])

    def test_single_line_and_tiny_ink_are_never_dropped(self):
        self.assertEqual(drop_dim_lines([("Sama.", 150, 900)]), ["Sama."])
        self.assertEqual(drop_dim_lines([("Mm.", 100, 40), ("Jasna.", 235, 900)]), ["Mm.", "Jasna."])

    def test_ink_brightness_takes_the_median_of_letter_pixels_only(self):
        lum = np.full((20, 40), 30.0, dtype=np.float32)
        lum[5:10, 5:25] = 200.0
        ink = lum > 100
        bright, n = ink_brightness(lum, ink, (0, 0, 40, 20))
        self.assertEqual((bright, n), (200.0, 100))
        self.assertEqual(ink_brightness(lum, ink, (30, 12, 8, 6)), (0.0, 0))

    def test_text_on_ink_skips_the_darker_line(self):
        lum = np.full((60, 200), 20.0, dtype=np.float32)
        ink = np.zeros((60, 200), dtype=bool)
        lum[5:25, 10:190] = 235.0   # jasna linia
        lum[35:55, 40:160] = 190.0  # ciemniejsza
        # litery to cienkie kreski (filtr tablic odrzuca pełne plamy)
        ink[5:25, 10:190:3] = True
        ink[35:55, 40:160:3] = True
        ocr = AppleVisionOcr()
        ocr._run_items = lambda *_a, **_k: [
            (0.1, 10, "Hey, Billy!", (10, 5, 180, 20), 0.9),
            (0.6, 40, "Yo, what's up?", (40, 35, 120, 20), 0.9),
        ]
        self.assertEqual(ocr._text_on_ink(None, ink, lum), "Hey, Billy!")
        # bez jasności kadru zachowanie jak dotąd: obie linie
        self.assertEqual(ocr._text_on_ink(None, ink), "Hey, Billy! Yo, what's up?")


if __name__ == "__main__":
    unittest.main()
