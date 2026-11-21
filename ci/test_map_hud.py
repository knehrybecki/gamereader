"""Log 21.11 15:06 (GTA VI, mapa i skaner pojazdu): lektor czytał podpowiedzi przycisków mapy („DODAJ…”, „Dodać”,
„itiz. ii…” z „DODAJ ZNACZNIK”), a skrót wyrzucił kwestię „Ostatni, dajesz.” i zostawił etykietę skanera
„KOSZT REJESTRACJI”."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine, RecurringFragments, strip_hud_prompts


class MapHudPromptTest(unittest.TestCase):
    def test_map_buttons_are_removed(self):
        self.assertEqual(strip_hud_prompts("WYŚRODKUJ WIDOK 2D DODAJ ZNACZNIK WYBIERZ CEL PODRÓŻY WYJDŹ"), "")
        self.assertEqual(strip_hud_prompts("DODAJ ZNACZNIK"), "")

    def test_pause_menu_buttons_are_removed(self):
        self.assertEqual(strip_hud_prompts("STUDIO FOTOGRAFICZNE • WYBIERZ"), "")

    def test_vehicle_scanner_labels_leave_the_dialogue(self):
        self.assertEqual(strip_hud_prompts("NADAJNIK WARTOŚĆ Ostatni, dajesz. KOSZT REJESTRACJI"), "Ostatni, dajesz.")

    def test_same_words_in_dialogue_stay(self):
        for line in ("Dodaj trochę gazu.", "Wyjdź stąd!", "Ile jest wart ten nadajnik?", "Wyśrodkuj kierownicę."):
            self.assertEqual(strip_hud_prompts(line), line)


def subtitle_engine():
    engine = Engine.__new__(Engine)
    engine.mode = "ocr"
    engine._pl_subs_at = None
    engine._source_kind = lambda: "ps"
    engine._recurring = RecurringFragments()
    engine.speaking_full = engine.last_full = engine._ocr_candidate = ""
    engine.speaking_text = ""
    engine._speaking_parts = []
    engine._ocr_candidate_n = 0
    engine._seen_at = {}
    engine._sub_prev = None
    engine._spoken_folds = {}
    engine.confirm_frames = 1
    engine._junk_logged = ""
    engine._lead_junk = Mock(observe=Mock(), strip=lambda text: text)
    engine._grown_from_recent = lambda *_: None
    engine._recently_spoken = lambda *_: False
    engine._speculate = Mock()
    engine._offer_line = Mock()
    engine._timing = Mock()
    engine.emit = Mock()
    return engine


class MapJunkNotReadTest(unittest.TestCase):
    def feed(self, engine, text):
        for t in (10.0, 10.2, 10.5, 10.8):
            with patch("gamereader_engine.time.monotonic", return_value=t):
                engine._on_subtitle(text)

    def test_map_button_garbage_is_not_read(self):
        for junk in ("DODAJ.isi itiz.ii…", "DODAJ…", "DODA. itiz. ii..."):
            engine = subtitle_engine()
            self.feed(engine, junk)
            engine._offer_line.assert_not_called()
            engine._speculate.assert_not_called()

    def test_dialogue_next_to_scanner_labels_is_read(self):
        engine = subtitle_engine()
        self.feed(engine, "NADAJNIK WARTOŚĆ Ostatni, dajesz. KOSZT REJESTRACJI")
        engine._offer_line.assert_called_once_with("Ostatni, dajesz.", False)


class LineReplacedByJunkTest(unittest.TestCase):
    """21.11 15:07: „Nie warto.” (2 odczyty, 0,29 s) zastąpił śmieciowy odczyt „TO” — kwestia przepadła."""

    def test_clean_line_replaced_by_junk_read_is_still_read(self):
        engine = subtitle_engine()
        for t, text in ((10.0, "Nie warto."), (10.15, "Nie warto."), (10.29, "TO")):
            with patch("gamereader_engine.time.monotonic", return_value=t):
                engine._on_subtitle(text)
        engine._offer_line.assert_called_once_with("Nie warto.", False)

    def test_truncated_reread_of_same_line_is_not_read_twice(self):
        engine = subtitle_engine()
        full = "Tak. Ten jest mój… Więc jeśli chcesz podpierdolić którąś z moich fur, to sobie odpuść."
        for t, text in ((10.0, full), (10.15, "Tak. Ten jest mój… Więc jeśli chcesz pod")):
            with patch("gamereader_engine.time.monotonic", return_value=t):
                engine._on_subtitle(text)
        engine._offer_line.assert_not_called()


if __name__ == "__main__":
    unittest.main()
