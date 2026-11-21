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
    def setUp(self):
        # dawne skracanie/wyrzucanie (do 21.11): logika została, domyślnie wyłączona przez READ_EVERY_LINE
        patcher = patch("gamereader_engine.READ_EVERY_LINE", False)
        patcher.start()
        self.addCleanup(patcher.stop)

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


class QueueVariantsTest(unittest.TestCase):
    """Dwa odczyty OCR tego samego napisu w kolejce nie są czytane jako dwie kwestie (18.11: 4,7 s na
    „e bul o wydarzenie lata dla nas, dzieciaków. e E t wydarzenie lata dla…”, potem 3 kwestie pominięte)."""

    def take(self, texts):
        eng = engine_with_queue([0.5] * len(texts))
        eng._queue = [(t, False, t) for t in texts]
        eng._seen_at = {t: 99.5 for t in texts}
        with patch.object(ge.time, "monotonic", return_value=100.0):
            return eng._take_pending()

    def test_garbled_variants_merge_into_best_reading(self):
        item = self.take(["e E t wydarzenie lata dla nas, dzieciaków.", "e bul o wydarzenie lata dla nas, dzieciaków."])
        self.assertEqual(item[3], ["e bul o wydarzenie lata dla nas, dzieciaków."])  # lepszy odczyt (więcej słów)

    def test_different_lines_are_both_read(self):
        item = self.take(["Mieli też przepiękną karuzelę.", "Kiedyś nocą się tam zakradliśmy z kumplem."])
        self.assertEqual(len(item[3]), 2)

    def test_short_line_is_not_a_variant_of_longer_one(self):
        self.assertFalse(ge.ocr_variants("Hej.", "Hej. Mam samochód tego Raula i jadę pod wskazany adres."))


class GrowingSubtitleTest(unittest.TestCase):
    """Napis, który OCR złapał w połowie („Hej.”), a chwilę później w całości („Hej. Mam samochód…”), lektor
    czyta raz — od razu pełną wersję (18.11: „Hej.” i zaraz „Hej. Mam samochód tego Raula…”)."""

    def test_longer_version_replaces_short_one(self):
        eng = engine_with_queue([0.6, 0.2])
        texts = ["Mam samochód Raula.", "Mam samochód Raula. Jadę pod wskazany adres."]
        eng._queue = [(t, False, t) for t in texts]
        eng._seen_at = {texts[0]: 99.4, texts[1]: 99.8}
        eng._prefetch_parts = [texts[0]]  # zsyntezowany zawczasu sam początek — i tak ustępuje pełnej wersji
        with patch.object(ge.time, "monotonic", return_value=100.0):
            item = eng._take_pending()
        self.assertEqual(item[3], [texts[1]])

    def test_grown_version_waiting_before_playback(self):
        eng = engine_with_queue([0.2])
        full = "Hej. Mam samochód tego Raula i jadę pod wskazany adres."
        eng._queue = [(full, False, full)]
        self.assertTrue(eng._grown_version("Hej."))
        self.assertFalse(eng._grown_version("Super. Jestem w drodze."))

    def test_extends_needs_real_prefix(self):
        self.assertTrue(ge.subtitle_extends("Hej.", "Hej. Mam samochód tego Raula."))
        self.assertFalse(ge.subtitle_extends("Uwaga!", "Skoczyłem, kurwa."))
        self.assertFalse(ge.subtitle_extends("Hej.", "Hej."))


class FilmTempoTest(unittest.TestCase):
    def test_game_lektor_target_is_a_bit_faster_than_film(self):
        # filmy z lektorem 9,8–10,9 liter/s; dialogi GTA szybsze — 18.11 lektor „nie wyrabia” przy 11
        self.assertEqual(ge.OMNI_TARGET_LPS, 12.0)


class ReadyAudioLookupTest(unittest.TestCase):
    """Dźwięk zsyntezowany zawczasu (przy pierwszym odczycie OCR) trafia do kwestii także, gdy tekst w kolejce jest
    wariantem tego samego napisu (18.11: „Czyli jesteśmy w ekipie?” syntezowany od nowa po poprzedniej kwestii)."""

    def engine(self, ready):
        eng = ge.Engine.__new__(ge.Engine)
        eng._ready_lock = threading.Lock()
        eng._ready = dict(ready)
        eng.logs = []
        eng._timing = eng.logs.append
        return eng

    def test_exact_text(self):
        eng = self.engine({"Chyba tak.": "audio"})
        self.assertEqual(eng._pop_ready("Chyba tak."), "audio")

    def test_variant_of_same_subtitle(self):
        eng = self.engine({"Czyli jesteśmy w ekipie": "audio"})
        self.assertEqual(eng._pop_ready("Czyli jesteśmy w ekipie?"), "audio")
        self.assertEqual(eng._ready, {})

    def test_other_line_is_not_taken(self):
        eng = self.engine({"Myślisz, że nam pomoże?": "audio"})
        self.assertIsNone(eng._pop_ready("Chyba tak."))


if __name__ == "__main__":
    unittest.main()
