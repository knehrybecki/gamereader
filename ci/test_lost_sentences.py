"""25.11 (GTA VI, użytkownik: „gubi zdania”): dwa filtry wycinały prawdziwe kwestie.

1. „Etykieta z mapy” (RecurringLead) nauczyła się zwykłych słów („wiesz”, „panie”, „mój”, „sklep”) i przez
   podobieństwo wycinała początki kwestii („Nie, nie! Ma kuloodporną szybę!” → „kuloodporną szybę!”) albo całe
   kwestie („Ta, pewnie.”, „Ładnie.”).
2. „Odczyt bez polskich słów” znał ~670 słów — 91 pominięć w 40 minut gry, co trzecie to prawdziwa kwestia
   („Potrzebuję tego auta.”, „Kierowco, zatrzymaj pojazd!”)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge
from gamereader_engine import Engine, RecurringFragments, RecurringLead

# lead_junk.json użytkownika z 25.11
LEARNED = ["kenton", "poludniowe", "zastrz", "ouon", "sklep", "ycs", "dook", "polnocne", "bret", "rzuc", "moj",
           "rydeme", "wiesz", "panie", "waink", "vbfightr", "randy", "beware", "annis", "wytrzymalosg", "mtws",
           "bayside", "trening", "zaaw"]


def learned_lead():
    lead = RecurringLead()
    lead.junk.extend(LEARNED)
    return lead


class DialogueIsNotAMapLabelTest(unittest.TestCase):
    def test_lines_cut_on_2026_11_25_stay_whole(self):
        lead = learned_lead()
        for line in (
            "Nie, nie! Ma kuloodporną szybę! Wal w opony i silnik!",
            "Pewnie. Co, myślisz, że odstawiłbym taki cyrk dla jednej torby szmalu?",
            "Nie wierzę, że dojechałeś tym tutaj.",
            "No cóż, wiesz, jak to jest…",
            "O nie! Tobie zupełnie odwaliło!",
            "To nie było konieczne! Wypad!",
            "No weź, daj mi spokój, proszę cię!",
            "Zawieź Raula do punktu dostawy.",
            "Pani od antyków?",
        ):
            self.assertEqual(lead.strip(line), line)

    def test_whole_lines_are_not_dropped(self):
        lead = learned_lead()
        for line in ("Ta, pewnie.", "Ładnie.", "Ta, no wiesz…", "Rzuć to.", "Mój Boże!"):
            self.assertEqual(lead.strip(line), line)

    def test_common_dialogue_word_is_never_learned(self):
        lead = RecurringLead()
        lead.observe("Wiesz Jason nie odbiera od rana.")
        lead.observe("Wiesz Lucia miała dziś ciężki dzień.")
        self.assertEqual(lead.strip("Wiesz Jason nie odbiera od rana."), "Wiesz Jason nie odbiera od rana.")

    def test_real_labels_are_still_removed(self):
        lead = learned_lead()
        self.assertEqual(lead.strip("ZAAW Nadal próbujemy na spokojnie?"), "Nadal próbujemy na spokojnie?")
        self.assertEqual(lead.strip("ZAAV Nadal próbujemy na spokojnie?"), "Nadal próbujemy na spokojnie?")  # wariant OCR
        self.assertEqual(lead.strip("VBEWA Nie masz pojęcia, co ci się trafiło, co?"), "Nie masz pojęcia, co ci się trafiło, co?")
        self.assertEqual(lead.strip("X RydeMe Podróż ukończona"), "Podróż ukończona")
        self.assertEqual(lead.strip("Sklep."), "")

    def test_short_dialogue_word_after_a_label_stays(self):
        self.assertEqual(learned_lead().strip("Kenton Co tam słychać?"), "Co tam słychać?")


class PolishLineIsNotJunkTest(unittest.TestCase):
    def test_real_lines_skipped_on_2026_11_25_count_as_polish(self):
        for line in (
            "Potrzebuję tego auta.", "Kierowco, zatrzymaj pojazd!", "Wiemy, gdzie jedziemy?", "Tu zostawimy auto.",
            "Ale ranny ptaszek.", "Potrzebujecie pracy, co?", "Och! Biedne autko!", "Jazda, jazda! Wypierdalaj!",
            "Ranny funkcjonariusz! Kurde!", "Hej! Doktorze Billu!", "Pompa paliwowa, skarbie, pompa.",
        ):
            self.assertTrue(ge.mostly_polish_words(line), line)

    def test_garbage_and_zone_names_do_not(self):
        for line in (
            "Thy co wedy atak eles.", "nony Rnp mae o sason rEaera", "otudniowe hrabstwo Vice-Dale",
            "e hrabstwo Vice-Dale", "LEONIDA BLAST VBFIHTR", "i i L i", "Mafa WYBIERZCE Centrum",
        ):
            self.assertFalse(ge.mostly_polish_words(line), line)

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

    def test_engine_reads_the_line(self):
        engine = self._engine()
        for stamp in (10.0, 10.45):
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle("Kierowco, zatrzymaj pojazd!")
        engine._offer_line.assert_called_once_with("Kierowco, zatrzymaj pojazd!", False)

    def test_engine_still_skips_garbage(self):
        engine = self._engine()
        for stamp in (30.0, 30.45):
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle("Thy co wedy atak eles.")
        engine._offer_line.assert_not_called()


if __name__ == "__main__":
    unittest.main()
