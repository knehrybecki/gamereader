"""24.11: lektor rusza dopiero, gdy pierwszy fragment jest zsyntezowany (~0,74 s + 8,5 ms na znak, log silnika).
Kwestia z kilku zdań ma krótszy pierwszy fragment — o ile obie części mają co najmniej 25 znaków i reszta zdąży się
zsyntezować, zanim lektor przeczyta początek (inaczej między fragmentami byłaby dziura)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import lektor_segments


def texts(line):
    return [seg for seg, _mood in lektor_segments(line)]


class FirstSegmentTest(unittest.TestCase):
    def test_multi_sentence_line_starts_with_first_sentence(self):
        self.assertEqual(texts("Nie, nie, nie. Forsa jest czysta. Forsa jest czysta! Czysta!"),
                         ["Nie, nie, nie. Forsa jest czysta.", "Forsa jest czysta! Czysta!"])

    def test_short_rest_stays_with_the_start(self):
        # „Przysięgam na babci!” samo brzmiałoby sztucznie (krótsze niż 25 znaków)
        self.assertEqual(texts("Ciśniemy maszyna, takie z nas kozaki! Przysięgam na babci!"),
                         ["Ciśniemy maszyna, takie z nas kozaki! Przysięgam na babci!"])

    def test_short_line_is_one_fragment(self):
        self.assertEqual(texts("Wygląda jak dobra okazja."), ["Wygląda jak dobra okazja."])
        self.assertEqual(texts("Kurde! Chyba przebijałaś się ta maszyna przez lity beton, niunia!"),
                         ["Kurde! Chyba przebijałaś się ta maszyna przez lity beton, niunia!"])

    def test_start_long_enough_to_cover_synthesis_of_the_rest(self):
        # 26 znaków to ~1,7 s czytania — za mało na syntezę 80 znaków; początek bierze drugie zdanie
        line = ("Dobra, słuchaj mnie teraz. Jedziemy na przystań. "
                "Tam czeka łódź, którą wypłyniemy daleko w morze, zanim gliny się obudzą.")
        segs = texts(line)
        self.assertEqual(segs[0], "Dobra, słuchaj mnie teraz. Jedziemy na przystań.")
        self.assertEqual(" ".join(segs), line)


if __name__ == "__main__":
    unittest.main()
