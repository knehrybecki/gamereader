"""Lektor jak w filmie: czyta wszystkie kwestie, przy zaległościach skraca i przyspiesza, zamiast wyrzucać całe.

18.11 (zwiastun GTA VI): z 14 kwestii 4 wypadły w całości („nadganiam: pomijam…”) — kolejka wyrzucała każdą
kwestię starszą niż 2 s, a lektor czyta jedną ~4 s; spóźnienie liczone bez celowego czekania na postać wychodziło
~1,5 s, więc skracanie (od 2 s) i przyspieszanie się nie włączały. Do tego „A kogo tu stróże prawa przywiali.”
odpadało jako „odczyt bez polskich słów”."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


def engine_with_queue(ages, now=100.0):
    eng = ge.Engine.__new__(ge.Engine)
    eng.pending_lock = threading.Lock()
    eng.has_pending = threading.Event()
    eng.catch_up, eng.catch_up_keep = True, 2
    eng.brain = None
    eng._prefetch_parts = None
    eng.logs = []
    eng._timing = eng.logs.append
    texts = [f"Kwestia numer {i} z treścią rozmowy." for i in range(len(ages))]
    eng._queue = [(t, False, t) for t in texts]
    eng._seen_at = {t: now - age for t, age in zip(texts, ages)}
    return eng


class CatchUpQueueTest(unittest.TestCase):
    def test_line_from_few_seconds_ago_is_still_read(self):
        eng = engine_with_queue([3.5, 0.5])
        with patch.object(ge.time, "monotonic", return_value=100.0):
            item = eng._take_pending()
        self.assertTrue(item[0].startswith("Kwestia numer 0"))
        self.assertFalse(any("nadganiam" in line for line in eng.logs))

    def test_really_stale_line_is_dropped(self):
        eng = engine_with_queue([ge.CATCH_UP_STALE_SEC + 1.0, 0.5])
        with patch.object(ge.time, "monotonic", return_value=100.0):
            item = eng._take_pending()
        self.assertTrue(item[0].startswith("Kwestia numer 1"))

    def test_stale_threshold_matches_film_lektor(self):
        self.assertGreaterEqual(ge.CATCH_UP_STALE_SEC, 6.0)


class EffectiveLagTest(unittest.TestCase):
    def test_lag_counts_from_subtitle_even_when_smoothed_lag_is_low(self):
        eng = ge.Engine.__new__(ge.Engine)
        eng._lag = 1.2
        eng._seen_at = {"Kurde, to było najważniejsze wydarzenie lata.": 95.4}
        with patch.object(ge.time, "monotonic", return_value=100.0):
            self.assertAlmostEqual(eng._effective_lag(["Kurde, to było najważniejsze wydarzenie lata."]), 4.6)

    def test_fresh_line_keeps_smoothed_lag(self):
        eng = ge.Engine.__new__(ge.Engine)
        eng._lag = 1.2
        eng._seen_at = {}
        with patch.object(ge.time, "monotonic", return_value=100.0):
            self.assertAlmostEqual(eng._effective_lag(["Nowa kwestia."]), 1.2)


class PolishWordsTest(unittest.TestCase):
    def test_plain_polish_line_counts_as_polish(self):
        self.assertGreaterEqual(ge.polish_common_words("A kogo tu stróże prawa przywiali."), 2)

    def test_garbage_has_no_common_words(self):
        self.assertLess(ge.polish_common_words("teteraizp ez esen obalom op oelelzid aąl"), 2)
        self.assertLess(ge.polish_common_words("Thy co wedy atak eles."), 2)


if __name__ == "__main__":
    unittest.main()
