"""Ściszanie gry pod lektorem jak w filmie z lektorem.

Pomiar 18.11 („Królewska niespodzianka”, „Protektor”): oryginał 11–13 dB pod lektorem, między kwestiami lektora
średnio 1,5 s. Gra wraca do pełnej głośności dopiero, gdy lektor milczy dłużej — bez „pompowania” w dialogu."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


class FakeTap:
    def __init__(self):
        self.gains = []

    def set_gain(self, value):
        self.gains.append(round(value, 2))


def engine(amount):
    eng = ge.Engine.__new__(ge.Engine)
    eng.duck_amount = amount
    eng._duck_timer = None
    eng._tap = FakeTap()
    eng.source_info = None
    return eng


@unittest.skipIf(sys.platform == "win32", "Windows ścisza przez mikser systemu")
class DuckingTest(unittest.TestCase):
    def test_default_ducks_like_film(self):
        self.assertEqual(ge.DUCK_AMOUNT_DEFAULT, 75)  # 0,25 = -12 dB
        eng = engine(ge.DUCK_AMOUNT_DEFAULT)
        eng._duck_on()
        self.assertEqual(eng._tap.gains, [0.25])

    def test_game_stays_ducked_between_close_lines(self):
        eng = engine(75)
        with patch.object(ge, "DUCK_HOLD_SEC", 0.2):
            eng._duck_on()
            eng._duck_release()
            time.sleep(0.1)
            eng._duck_on()  # następna kwestia przed końcem przytrzymania
            eng._duck_release()
            time.sleep(0.1)
            self.assertNotIn(1.0, eng._tap.gains)  # gra nie podskoczyła między kwestiami
            time.sleep(0.25)
            self.assertEqual(eng._tap.gains[-1], 1.0)  # po dłuższej ciszy wraca

    def test_hold_matches_film_gap(self):
        self.assertGreaterEqual(ge.DUCK_HOLD_SEC, 1.5)


if __name__ == "__main__":
    unittest.main()
