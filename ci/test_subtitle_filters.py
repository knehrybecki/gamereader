"""Regresje OCR z sesji LiveDub; bez modeli, dźwięku i przechwytywania ekranu."""
import sys
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine, RecurringFragments, strip_lead_junk, screen_junk_level, same_utterance


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


if __name__ == "__main__":
    unittest.main()
