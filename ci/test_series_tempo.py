"""21.11 — tempo jak w ustawieniach GameReadera, które pokazał użytkownik: pierwsza kwestia w serii ×1,00,
każda następna (dialog zmienia się w trakcie mowy lektora) ×1,20; 25.11 „za wolny, przyspiesz”: ×1,10 i ×1,35, potem „jeszcze trochę”: ×1,20 i ×1,45. Do tego log napisu, który przepadł przed
potwierdzeniem (21.11 „Coś podać?” i „Jezu Chryste, jeszcze kogoś zabijesz!” zniknęły bez śladu w logu)."""
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge
from gamereader_engine import Engine


def voicestudio_lektor():
    lk = ge.MaleLektor.__new__(ge.MaleLektor)
    lk.tts_scale = 0.92
    lk.cps1 = ge.LEKTOR_CPS_PRIOR
    lk.cloud = SimpleNamespace(ready=True, failed_until=0.0)
    return lk


class SeriesBoostTest(unittest.TestCase):
    LINE = "Powiedziała, że możesz mi pomóc z autem."

    def test_first_line_of_series_reads_at_base_tempo(self):
        self.assertEqual(voicestudio_lektor().line_boost(self.LINE, 4.0, lag=0.0), 1.20)

    def test_next_line_in_series_reads_at_145(self):
        self.assertEqual(ge.LEKTOR_NEXT_LINE_BOOST, 1.45)
        self.assertEqual(voicestudio_lektor().line_boost(self.LINE, 4.0, lag=0.0, series=True), 1.45)

    def test_series_tempo_is_the_ceiling(self):
        self.assertEqual(voicestudio_lektor().line_boost(self.LINE, 1.0, lag=5.0, series=True), 1.50)


def queue_engine():
    engine = Engine.__new__(Engine)
    engine.pending_lock = threading.Lock()
    engine._queue = []
    engine.has_pending = threading.Event()
    engine._lag = 0.0
    engine.speaking_text = ""
    engine._speaking_parts = []
    return engine


class SeriesMarkTest(unittest.TestCase):
    def test_line_arriving_while_lektor_speaks_is_next_in_series(self):
        engine = queue_engine()
        engine.speaking_text = "Przepraszam. Jestem Lucia, znajoma Vicki."
        engine._enqueue(("Powiedziała, że możesz mi pomóc z autem.", False, "Powiedziała, że możesz mi pomóc z autem."))
        self.assertTrue(engine._in_series(["Powiedziała, że możesz mi pomóc z autem."]))
        # wariant OCR tej samej kwestii też
        self.assertTrue(engine._in_series(["Powiedziała, że możesz mi pomóc z autem"]))

    def test_line_after_silence_starts_a_new_series(self):
        engine = queue_engine()
        engine._enqueue(("Cholera, oczy dookoła głowy!", False, "Cholera, oczy dookoła głowy!"))
        self.assertFalse(engine._in_series(["Cholera, oczy dookoła głowy!"]))

    def test_line_arriving_while_next_line_is_being_prepared_is_in_series(self):
        engine = queue_engine()
        engine._speaking_parts = ["Coś ci o tym wspominała?"]
        engine._enqueue(("W porządku, to wrócę później.", False, "W porządku, to wrócę później."))
        self.assertTrue(engine._in_series(["W porządku, to wrócę później."]))

    def test_old_marks_expire(self):
        engine = queue_engine()
        engine.speaking_text = "Hej."
        engine._enqueue(("Odczep się, dobra?", False, "Odczep się, dobra?"))
        engine._series_at["Odczep się, dobra?"] = time.monotonic() - ge.SERIES_MEMORY_SEC - 1
        self.assertFalse(engine._in_series(["Odczep się, dobra?"]))


class DroppedCandidateLogTest(unittest.TestCase):
    def engine(self, candidate):
        engine = Engine.__new__(Engine)
        engine._ocr_candidate = candidate
        engine._ocr_candidate_n = 1
        engine._ocr_started = 10.0
        engine._junk_logged = ""
        engine.speaking_text = ""
        engine._speaking_parts = []
        engine._spoken_folds = {}
        engine._timing = Mock()
        return engine

    def test_replaced_unfinished_line_is_logged(self):
        engine = self.engine("Jezu Chryste, jeszcze kogoś")
        engine._note_dropped_candidate("Ci Watkins", 10.3)
        engine._timing.assert_called_once()
        self.assertIn("przepadł", engine._timing.call_args[0][0])

    def test_replaced_complete_line_is_read(self):
        # 25.11: ze słownikiem napisów „Jezu Chryste, jeszcze kogoś zabijesz!” to pewny odczyt — idzie do lektora
        engine = self.engine("Jezu Chryste, jeszcze kogoś zabijesz!")
        engine._offer_line = Mock()
        engine._note_dropped_candidate("Ci Watkins", 10.3)
        engine._offer_line.assert_called_once_with("Jezu Chryste, jeszcze kogoś zabijesz!", False)

    def test_grown_line_is_not_a_loss(self):
        engine = self.engine("Coś podać?")
        engine._note_dropped_candidate("Coś podać? Odczep się.", 10.3)
        engine._timing.assert_not_called()

    def test_ocr_variant_is_not_a_loss(self):
        engine = self.engine("Udai się do serwisu łodzi.")
        engine._note_dropped_candidate("Udaj się do serwisu fodzi.", 10.3)
        engine._timing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
