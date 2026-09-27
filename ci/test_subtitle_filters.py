"""Regresje OCR z sesji LiveDub; bez modeli, dźwięku i przechwytywania ekranu."""
import sys
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import (
    Engine, RecurringFragments, strip_lead_junk, screen_junk_level, same_utterance, ocr_reading_rank,
    trim_ocr_edges, condense_polish, lektor_speed_split, LEKTOR_MAX_RATE, LEKTOR_MAX_STRETCH,
    strip_known_prefix, ocr_reading_unsettled, repair_polish_ocr,
)


class SubtitleFiltersTest(unittest.TestCase):
    def test_transient_plate_fragment_needs_fresh_confirmation(self):
        engine = Engine.__new__(Engine)
        engine.mode = "ocr"
        engine._pl_subs_at = None
        engine._source_kind = lambda: "ps"
        engine._recurring = RecurringFragments()
        engine.speaking_full = engine.last_full = engine._ocr_candidate = ""
        engine._ocr_candidate_n = 0
        engine._seen_at = {}
        engine._sub_prev = None
        engine.confirm_frames = 1
        engine._grown_from_recent = lambda *_: None
        engine._recently_spoken = lambda *_: False
        engine._speculate = Mock()
        engine._offer_line = Mock()
        engine.emit = Mock()
        with patch("gamereader_engine.time.monotonic", return_value=10.0):
            engine._on_subtitle("Andersi,")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=12.0):
            engine._on_subtitle("Andersi,")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=12.15):
            engine._on_subtitle("Andersi,")
        engine._offer_line.assert_called_once()
        engine._offer_line.reset_mock()
        engine._sub_prev = None
        with patch("gamereader_engine.time.monotonic", return_value=13.0):
            engine._on_subtitle("To tutaj.")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=13.45):
            engine._on_subtitle("To tutaj.")
        engine._offer_line.assert_called_once_with("To tutaj.", False)

    def test_short_dialogue_ocr_variants_are_not_repeated(self):
        for variant in ("To tutai.", "To tutal."):
            self.assertTrue(same_utterance("To tutaj.", variant))
            self.assertTrue(same_utterance(variant, "To tutaj."))
        for different in ("To jutro.", "To tam.", "Nie tutaj."):
            self.assertFalse(same_utterance("To tutaj.", different))
        self.assertFalse(same_utterance("On stoi.", "On stój."))

    def test_long_ocr_prefixes_do_not_turn_dialogue_into_watermark(self):
        dialogue = "Może kiedyś przy browarku wyłuszczę ci, jak działa ten świat."
        fragments = RecurringFragments()
        for prefix in ("OrtleLLI PrEME ", "PRZEGLADANIE ", ""):
            text = prefix + dialogue
            self.assertEqual(fragments.strip(text), text)

    def test_logged_hud_prefixes_preserve_dialogue(self):
        dialogue = "Więc raczej odstawiasz przekręt z zadłużeniem."
        fragments = RecurringFragments()
        for prefix in ("SIELE ", "tANG ", "SĘENCE ", "PARNE ", "PLENE ", ""):
            self.assertEqual(fragments.strip(strip_lead_junk(prefix + dialogue)), dialogue)

    def test_watermark_on_different_dialogues_is_still_removed(self):
        watermark = "Subskrybuj nasz kanał już teraz"
        fragments = RecurringFragments()
        for dialogue in ("Jedź szybko do garażu, bo nas dogonią.",
                         "Muszę kupić coś dobrego na dzisiejszą kolację."):
            text = dialogue + " " + watermark
            self.assertEqual(fragments.strip(text), text)
        dialogue = "Przestań strzelać, tam są nasi ludzie!"
        self.assertEqual(fragments.strip(dialogue + " " + watermark), dialogue)
        self.assertEqual(fragments.strip(watermark), "")

    def test_menu_from_live_log_is_rejected(self):
        self.assertEqual(screen_junk_level("PRZEGLĄDAJ L R UKRYJ MENU"), "junk")

    def _engine(self):
        engine = Engine.__new__(Engine)
        engine.mode = "ocr"
        engine._pl_subs_at = None
        engine._source_kind = lambda: "ps"
        engine._recurring = RecurringFragments()
        engine.speaking_full = engine.last_full = engine._ocr_candidate = ""
        engine._ocr_candidate_n = 0
        engine._ocr_samples = []
        engine._seen_at = {}
        engine._sub_prev = None
        engine.confirm_frames = 1
        engine._grown_from_recent = lambda *_: None
        engine._recently_spoken = lambda *_: False
        engine._speculate = Mock()
        engine._offer_line = Mock()
        engine.emit = Mock()
        engine._junk_logged = ""
        engine._timing = Mock()
        return engine

    def test_clean_line_is_read_immediately(self):
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=10.0):
            engine._on_subtitle("To się nazywa kapitalizm.")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=10.45):
            engine._on_subtitle("To się nazywa kapitalizm.")
        engine._offer_line.assert_called_once_with("To się nazywa kapitalizm.", False)

    def test_blurred_first_frame_waits_for_a_cleaner_reading(self):
        engine = self._engine()
        frames = [
            (10.00, "spontanfcznego ożywienia zwłok, które zaczęły straszyåwezyalich wøköt, takø"),
            (10.18, "spontanfcznego ozywienia zwłok, które zaczęły straszydwezjafichwkól, taką"),
            (10.51, "spontanicznego ożywienia zwłok, które zaczęły straszyć wezykich wekö, tak?"),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        engine._offer_line.assert_not_called()
        better = "spontanicznego ożywienia zwłok, które zaczęły straszyć wezystich woket, tak?"
        self.assertGreater(ocr_reading_rank(better), ocr_reading_rank(frames[0][1]))
        with patch("gamereader_engine.time.monotonic", return_value=10.70):
            engine._on_subtitle(better)
        spoken = engine._offer_line.call_args[0][0]
        self.assertIn("spontanicznego", spoken)
        self.assertNotIn("spontanf", spoken)

    def test_first_misspelled_frame_yields_to_the_next(self):
        engine = self._engine()
        frames = [
            (40.00, "Bobra, Idziemy."),
            (40.16, "Dobra, idziemy."),
            (40.70, "Dobra, idziemy."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        spoken = [call.args[0] for call in engine._offer_line.call_args_list]
        self.assertTrue(spoken)
        self.assertTrue(all(text == "Dobra, idziemy." for text in spoken))

    def test_garbled_car_line_is_replaced_by_the_clear_one(self):
        engine = self._engine()
        frames = [
            (50.00, "Weżmy aaro Amtindy."),
            (50.15, "Weźmy auto Amandy."),
            (50.70, "Weźmy auto Amandy."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        spoken = [call.args[0] for call in engine._offer_line.call_args_list]
        self.assertTrue(spoken)
        self.assertTrue(all("auto Amandy" in text and "Amtindy" not in text for text in spoken))

    def test_garbled_reread_without_polish_words_is_skipped(self):
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=30.0):
            engine._on_subtitle("Thy co wedy atak eles.")
        engine._offer_line.assert_not_called()

    def test_leading_ocr_mark_is_removed(self):
        self.assertEqual(trim_ocr_edges("¡To dlatego siedzisz w Vinewood?"), "To dlatego siedzisz w Vinewood?")

    def test_no_to_is_not_condensed_away(self):
        text = "No to morałem dzisiejszej lekcji niech będzie pokora."
        self.assertTrue(condense_polish(text, level=1).lower().startswith("no to"))

    def test_flickering_variants_are_not_joined_into_one_line(self):
        engine = self._engine()
        frames = [
            (20.00, "z mógł kantować ludzli na tymzarablać. tAredy bedkles"),
            (20.16, "Wtedy będklesz mógł kantować ludili na tymzarablać."),
            (20.70, "Wtedy będziesz mógł kantować ludzi i na tym zarabiać."),
            (20.90, "Wtedy będziesz mógł kantować ludzi i na tym zarabiać."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        engine._offer_line.assert_called_once()
        spoken = engine._offer_line.call_args[0][0]
        self.assertIn("będziesz", spoken)
        self.assertNotIn("bedkles", spoken)

    def test_game_line_is_synthesized_while_ocr_settles(self):
        # sesja 15:15: bez syntezy na zapas lektor startował 1,3 s po napisie zamiast ~0,4 s
        engine = self._engine()
        frames = [
            (12.14, "Jeśli mam to zrobić, to musisz podjechać olizej."),
            (12.30, "Jeśli mam to zrobić, to musisz podjechać bliżej."),
            (12.60, "Jeśli mam to zrobić, to musisz podjechać bliżej."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        spoken = engine._offer_line.call_args[0][0]
        speculated = [call.args[0] for call in engine._speculate.call_args_list]
        self.assertEqual(speculated[-1], spoken)
        self.assertEqual(len(speculated), len(set(speculated)))

    def test_clean_line_read_once_before_hud_frames_is_not_lost(self):
        # sesja 15:17: „Zabieraj stąd swoje dupsko.” raz, potem „Mitun”, „lin” — lektor milczał
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=38.00):
            engine._on_subtitle("Zabieraj stąd swoje dupsko.")
            engine._flush_candidate()
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=38.45):
            engine._on_subtitle("Mitun")
            engine._flush_candidate()
        engine._offer_line.assert_called_once_with("Zabieraj stąd swoje dupsko.", False)

    def test_unpunctuated_single_glimpse_is_not_flushed(self):
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=30.00):
            engine._on_subtitle("Hey pie chcę ff")
        with patch("gamereader_engine.time.monotonic", return_value=30.60):
            engine._flush_candidate()
        engine._offer_line.assert_not_called()

    def test_frame_junk_before_known_line_is_cut(self):
        # sesja 15:15–15:17: napis z doklejonym z kadru przodem
        self.assertEqual(strip_known_prefix("Ubermach Tam! Tam! To moja łódź!", "Tam! Tam! To moja łódź!"),
                         "Tam! Tam! To moja łódź!")
        self.assertEqual(
            strip_known_prefix("są Andrese KRYSTEL Jeśli mam to zrobić, to musisz podjechać bliżej.",
                               "Jeśli mam to zrobić, to musisz podjechać bliżej."),
            "Jeśli mam to zrobić, to musisz podjechać bliżej.")
        # pierwsze zdanie kwestii to nie śmieć, nawet gdy pierwszy odczyt był ucięty
        self.assertIsNone(strip_known_prefix(
            "Dasz radę. Jak będzie gorąco, to w schowku jest klamna. Będę cię ostaniał.",
            "Jasz radę. Jak będzie gorąco, to w schowku jest klama. Będę cię"))

    def test_one_letter_polish_words_are_settled(self):
        self.assertFalse(ocr_reading_unsettled("O kurwa!"))
        self.assertFalse(ocr_reading_unsettled("O nie, to ty wybrałeś złą łajbę."))

    def test_exclamation_read_as_l_loses(self):
        self.assertGreater(ocr_reading_rank("Dobra! Idź, znajdź Jimmy'ego!"), ocr_reading_rank("Dobrał Idź, znajdź Jimmy'ego!"))
        self.assertGreater(ocr_reading_rank("Tylko nie silnik! Kurwa! Jebany silnik!"),
                           ocr_reading_rank("Tylko nie silnik! Kurwal Jebany silnik!"))

    def test_scigaj_gets_its_accent(self):
        self.assertEqual(repair_polish_ocr("Scigaj jacht."), "Ścigaj jacht.")

    def test_catchup_pace_stays_intelligible(self):
        # pace jak przy doganianiu z sesji 13:48 (boost 1,40, tts 0,92, żywy głos)
        speed, stretch = lektor_speed_split(0.53, "Zabierz Franklina blisko jachtu.")
        self.assertLessEqual(speed, LEKTOR_MAX_RATE)
        self.assertLessEqual(stretch, LEKTOR_MAX_STRETCH)
        self.assertLessEqual(speed * stretch, LEKTOR_MAX_RATE + 1e-6)
        # sesja 15:16: ×1,25 z atempo ×1,12 było niezrozumiałe — lektor filmowy nie szybciej niż ×1,15
        self.assertLessEqual(speed * stretch, 1.15 + 1e-6)
        self.assertLessEqual(stretch, 1.05 + 1e-6)


if __name__ == "__main__":
    unittest.main()
