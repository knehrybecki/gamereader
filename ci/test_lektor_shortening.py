"""Skracanie kwestii lektora z sesji GTA VI (Chrome, 09.10): bez modeli, dźwięku i ekranu."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from gamereader_engine import (
    AppleVisionOcr, Engine, MaleLektor, LEKTOR_MAX_RATE, LEKTOR_SPEED, drop_dim_lines, drop_detached_last, ink_brightness, repair_polish_ocr, strip_fillers, is_grunt_only, looks_polish, should_translate,
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
    engine.brain = None
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
        self.assertLessEqual(boost * LEKTOR_SPEED, LEKTOR_MAX_RATE + 0.05)  # boost zaokrąglany co 0,05


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


class DetachedLineTest(unittest.TestCase):
    """Kadr GTA VI z celem misji pod dialogiem (09.10)."""

    def test_mission_objective_below_dialogue_is_dropped(self):
        # zmierzone: trzy linie dialogu co ~55 px (wys. 44), cel misji 99 px niżej
        lines = [("I always said if aliens land,", (400, 160, 700, 43)),
                 ("I want 'em to land in the middle", (200, 215, 1100, 44)),
                 ("that's a first impression!", (500, 270, 500, 44)),
                 ("Beat a member of Billy's crew", (480, 369, 600, 58))]
        self.assertEqual([t for t, _b in drop_detached_last(lines)][-1], "that's a first impression!")
        self.assertEqual(len(drop_detached_last(lines)), 3)

    def test_tight_two_line_subtitles_are_kept(self):
        # zmierzone odstępy 1,1–1,2 wysokości linii
        lines = [("Hey, Billy!", (200, 270, 300, 46)), ("Yo, what's up?", (180, 326, 340, 50))]
        self.assertEqual(drop_detached_last(lines), lines)
        lines = [("Alright, good luck.", (185, 88, 770, 58)), ("I never eat shit.", (430, 148, 280, 40))]
        self.assertEqual(drop_detached_last(lines), lines)

    def test_single_line_is_kept(self):
        lines = [("Beat a member of Billy's crew", (480, 369, 600, 58))]
        self.assertEqual(drop_detached_last(lines), lines)


class TrailingRemarkTest(unittest.TestCase):
    LONG = "Zawsze mówiłem, że jeśli kosmici wylądują, chcę, żeby wylądowali w środku wyścigu TMC. Teraz, to pierwsze wrażenie!"

    def _engine(self, lag, info):
        engine = make_engine(FakeTranslator(self.LONG), 9.0)
        engine._lag = lag
        engine.brain = Mock(verdict=Mock(return_value={"dialog": 0.9, "info": info}))
        return engine

    def test_late_lektor_drops_a_trailing_remark_the_model_finds_empty(self):
        text, _s, _a, _b = self._engine(2.4, 0.05)._plan_line("I always said ... Now, that's a first impression!", True)
        self.assertTrue(text.endswith("wyścigu TMC."))

    def test_trailing_sentence_with_information_is_kept(self):
        # log 11:41: „Mamy tam mnóstwo wypożyczalni!” (4 słowa) wypadło tylko dlatego, że było krótkie
        engine = make_engine(FakeTranslator("Jeśli chcesz wziąć udział w akcji. Mamy tam mnóstwo wypożyczalni!"), 9.0)
        engine._lag = 3.0
        engine.brain = Mock(verdict=Mock(return_value={"dialog": 0.9, "info": 0.9}))
        text, _s, _a, _b = engine._plan_line("If you wanna get in on the action. We got plenty of rentals!", True)
        self.assertTrue(text.endswith("wypożyczalni!"))

    def test_without_the_model_only_a_two_word_remark_is_dropped(self):
        engine = make_engine(FakeTranslator("Wsiadaj do auta i jedź za mną do magazynu. Uważaj na gliniarzy!"), 9.0)
        engine._lag = 3.0
        text, _s, _a, _b = engine._plan_line("Get in the car and follow me to the warehouse. Watch out for cops!", True)
        self.assertTrue(text.endswith("gliniarzy!"))
        engine = make_engine(FakeTranslator("Wsiadaj do auta i jedź za mną do magazynu. Dobra, jadę."), 9.0)
        engine._lag = 3.0
        text, _s, _a, _b = engine._plan_line("Get in the car and follow me to the warehouse. Okay, going.", True)
        self.assertEqual(text, "Wsiadaj do auta i jedź za mną do magazynu.")

    def test_on_time_lektor_reads_the_remark(self):
        engine = self._engine(0.3, 0.05)
        text, _s, _a, _b = engine._plan_line("I always said ... Now, that's a first impression!", True)
        self.assertTrue(text.endswith("pierwsze wrażenie!"))

    def test_a_question_is_never_a_remark(self):
        engine = make_engine(FakeTranslator("Idziemy do magazynu. Czy ktoś ma klucz?"), 9.0)
        engine._lag = 4.0
        text, _s, _a, _b = engine._plan_line("We go to the warehouse. Does anyone have a key?", True)
        self.assertTrue(text.endswith("klucz?"))


class UntranslatedTest(unittest.TestCase):
    """Argos kopiuje angielską „sieczkę” z OCR prawie bez zmian — to nie może wejść do lektora (log 11:41)."""

    def test_echoed_english_garbage_is_dropped(self):
        src = "Shorila ae able to at flegst beat Ey. Shoulde able to at least beat T."
        engine = make_engine(FakeTranslator(src), 6.0)
        text, segments, _a, _b = engine._plan_line(src, True)
        self.assertEqual((text, segments), ("", []))

    def test_dialogue_with_repeated_words_is_kept(self):
        # log 11:41: „And there's Billy. Here, c'mon, c'mon.” — powtórzone „Cimon” to nie echo
        engine = make_engine(FakeTranslator("I jest Billy. Tutaj, Chon, Cimon, Cimon."), 6.0)
        text, _s, _a, _b = engine._plan_line("And there's Billy. Here, chon, Cimon, Cimon.", True)
        self.assertTrue(text.startswith("I jest Billy"))

    def test_real_translation_with_shared_names_is_kept(self):
        engine = make_engine(FakeTranslator("Jason i Lucia. Cześć."), 6.0)
        text, _s, _a, _b = engine._plan_line("Jason and Lucia. Hey.", True)
        self.assertEqual(text, "Jason i Lucia. Cześć.")
        engine = make_engine(FakeTranslator("Ricky, Sam i oni są tam. Wchodzisz?"), 6.0)
        text, _s, _a, _b = engine._plan_line("Ricky, Sam and them are down this way. You comin in?", True)
        self.assertTrue(text.startswith("Ricky, Sam"))


class EnglishPronounTest(unittest.TestCase):
    def test_english_pronoun_i_survives_ocr_repair(self):
        for src in ("Yeah. I look good in these.", "I always said if aliens land,", "I'm gon roll me a fat one.",
                    "Alright, good luck. I never eat shit."):
            self.assertEqual(repair_polish_ocr(src), src.replace("'", "'"))

    def test_junk_tokens_are_still_dropped_from_polish_text(self):
        self.assertEqual(repair_polish_ocr("Idziemy do domu I l").split(), ["Idziemy", "do", "domu"])


class GruntTest(unittest.TestCase):
    """Odgłosy („Mm-hmm”) lektor nie czyta — także w wersjach przekręconych przez OCR (log 09.10)."""

    GRUNTS = [
        "Mm-hmm.", "Mim-hmm.", "Min-hmm.", "Mimn-hmm.", "Minn-hmm.", "Mm-himm®", "Mim-hmim.", "Min-himm.",
        "Mm-hm.", "Mhm.", "Hmm.", "Mm: bm m.", "Uh-huh.", "Uh-uh.", "Woo-hoo!", "Woo!", "Whoa!", "Yee-haw!",
        "Aww.", "Psst.", "Mm-hmm, mm-hmm.",
    ]
    WORDS = ["Him and me.", "Mine.", "Minimum.", "Mom?", "Yeah.", "Nah.", "Mr. Jones.", "Dr. Smith.", "We win.",
             "Mimi is here.", "Nine."]

    def test_grunts_and_their_ocr_variants_are_removed(self):
        for grunt in self.GRUNTS:
            self.assertEqual(strip_fillers(grunt), "", grunt)

    def test_grunt_inside_a_line_is_cut_out_and_the_rest_kept(self):
        self.assertEqual(strip_fillers("Yeah. I look good in these. Mim-hmm®"), "Yeah. I look good in these.")
        self.assertEqual(strip_fillers("Woo-hoo! Lookin good!"), "Lookin good!")
        self.assertEqual(strip_fillers("Whoa, wait!"), "Wait!")

    def test_grunt_only_readings_are_flagged_but_short_real_words_are_not(self):
        for grunt in self.GRUNTS:
            self.assertTrue(is_grunt_only(grunt), grunt)
        for text in ("No.", "Go!", "Yeah.", "Mine.", "Mr. Jones.", "Yeah. Mm-hmm. I do."):
            self.assertFalse(is_grunt_only(text), text)

    def test_real_words_are_never_removed(self):
        for text in self.WORDS:
            self.assertEqual(strip_fillers(text), text, text)


class ShortEnglishTest(unittest.TestCase):
    """Krótkie angielskie kwestie bez słów z małej listy EN_COMMON szły do lektora nietłumaczone (log 12:04, test 09.10)."""

    def test_short_english_lines_are_sent_to_translation(self):
        for text in ("Hey, Billy!", "Hey. Billy!", "No way!", "Oh, shit.", "Billy, wait!", "Hold on.", "Nice one, Jay.",
                     "Where's Lucia?", "Run!", "Sure thing.", "Don't move.", "Cool. Stay there."):
            self.assertTrue(should_translate(text), text)

    def test_short_polish_lines_are_not(self):
        for text in ("Hej, Billy!", "Ty prowadzisz?", "Dawaj, dawaj.", "Nie, w prawo.", "To tutaj.", "Wsiadaj.", "Tego nie wiem.",
                     "Ruszaj się!", "Idziemy!", "Spokojnie, Jay.", "To moi kumple."):
            self.assertFalse(should_translate(text), text)

    def test_words_that_are_also_polish_are_no_english_evidence(self):
        from gamereader_engine import EN_DIALOGUE
        for word in ("ten", "most", "my", "by", "sam", "pan", "dom", "mama", "tak", "nie", "jak"):
            self.assertNotIn(word, EN_DIALOGUE, word)


class LooksPolishTest(unittest.TestCase):
    def test_english_line_with_a_polish_homograph_is_translated(self):
        # log 12:01: „Same” (pol. „same”) remisowało z „you” i angielski szedł do lektora bez tłumaczenia
        # (samo „Same, pleasure.” bez angielskiego słowa ze słownika zostaje nierozstrzygalne — granica reguły)
        for text in ("Nice to meet you guys. Same, pleasure.", "Nice to meet you guyse Same, pleasure."):
            self.assertTrue(should_translate(text), text)

    def test_polish_without_diacritics_stays_polish(self):
        for text in ("Dobrze, Charles, to my to podnosimy,", "Hej Jak leci?",
                     "Wsiadaj do auta i jedz za mna."):
            self.assertFalse(should_translate(text), text)


if __name__ == "__main__":
    unittest.main()
