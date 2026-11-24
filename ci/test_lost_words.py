"""19.11: lektor VoiceStudio losowo gubi słowa („Świetnie! Wreszcie…” → „Wreszcie…”, „raczej tobie potrzebna”
→ „raczej potrzebna”, całe drugie zdanie). Ta sama kwestia raz wychodzi dobrze, raz źle — więc nagranie jest
sprawdzane rozpoznawaniem mowy, a przy zgubionym słowie synteza idzie jeszcze raz."""
import sys
import unittest
from pathlib import Path
from collections import Counter
from unittest.mock import Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge
from gamereader_engine import lost_words


class LostWordsTest(unittest.TestCase):
    def test_dropped_words_are_found(self):
        self.assertEqual(lost_words("Świetnie! Wreszcie mogę to chapnąć.", "Wreszcie mogę to kapnąć."), ["swietnie"])
        self.assertEqual(
            lost_words("Będę z tobą szczera. Ostatnio to raczej tobie potrzebna jest socjalizacja.", "Będę z tobą szczera."),
            ["ostatnio", "raczej", "tobie", "potrzebna", "jest", "socjalizacja"],
        )

    def test_recognition_slips_are_not_losses(self):
        for text, heard in (
            ("Świetnie! Wreszcie mogę to chapnąć.", "Świetnie. Wreszcie mogę to kapnąć."),  # słowo podobne
            ("Wszystkim to rozpowiem, Bri.", "Wszystkim to rozpowiem, Bree."),
            ("Trzymaj się, Dobbs.", "Trzymaj się dobrze."),  # imię — rozpoznawanie i tak go nie zna
            ("No, i dbaj o nią.", "No dbaj nią."),  # krótkie słówka pomijamy
        ):
            self.assertEqual(lost_words(text, heard), [], text)

    def test_nothing_heard_from_short_line_is_not_judged(self):
        self.assertEqual(lost_words("Uch…", ""), [])


class RetryTest(unittest.TestCase):
    def _lektor(self, heard):
        lektor = ge.MaleLektor.__new__(ge.MaleLektor)
        lektor._cloud_prev = ("", 0.0)
        lektor.on_cloud_error = None
        lektor.cps1 = ge.LEKTOR_CPS_PRIOR
        lektor.verify = Mock(side_effect=heard)
        lektor._write_voice = Mock()
        cloud = Mock()
        cloud.rate = 24000
        cloud.error = ""
        first, second = np.full(24000, 0.1, np.float32), np.full(24000, 0.2, np.float32)
        cloud.synth.side_effect = [first, second]
        return lektor, cloud, first, second

    def test_line_with_lost_word_is_synthesized_again(self):
        lektor, cloud, _first, second = self._lektor(["Wreszcie mogę to chapnąć.", "Świetnie. Wreszcie mogę to chapnąć."])
        lektor._synth_cloud(cloud, "Świetnie! Wreszcie mogę to chapnąć.", Path("x.wav"), 1.0, 0.1)
        self.assertEqual(cloud.synth.call_count, 2)
        self.assertIs(lektor._write_voice.call_args.args[0], second)

    def test_good_line_is_not_repeated(self):
        lektor, cloud, first, _second = self._lektor(["Świetnie. Wreszcie mogę to chapnąć."])
        lektor._synth_cloud(cloud, "Świetnie! Wreszcie mogę to chapnąć.", Path("x.wav"), 1.0, 0.1)
        self.assertEqual(cloud.synth.call_count, 1)
        self.assertIs(lektor._write_voice.call_args.args[0], first)

    def test_retry_that_is_worse_keeps_the_first(self):
        lektor, cloud, first, _second = self._lektor(["Świetnie. Wreszcie to chapnąć.", "Świetnie."])
        lektor._synth_cloud(cloud, "Świetnie! Wreszcie mogę to chapnąć.", Path("x.wav"), 1.0, 0.1)
        self.assertIs(lektor._write_voice.call_args.args[0], first)

    def test_without_recognizer_line_goes_as_is(self):
        lektor, cloud, first, _second = self._lektor([])
        lektor.verify = None
        lektor._synth_cloud(cloud, "Świetnie! Wreszcie mogę to chapnąć.", Path("x.wav"), 1.0, 0.1)
        self.assertIs(lektor._write_voice.call_args.args[0], first)

    # 24.11: 213 z 404 powtórek niczego nie poprawiło (log silnika), a każda to ~1 s spóźnienia lektora
    def test_no_retry_when_next_line_is_waiting(self):
        lektor, cloud, first, _second = self._lektor(["Wreszcie mogę to chapnąć."])
        lektor._synth_cloud(cloud, "Świetnie! Wreszcie mogę to chapnąć.", Path("x.wav"), 1.0, 0.1, hurry=True)
        self.assertEqual(cloud.synth.call_count, 1)
        self.assertIs(lektor._write_voice.call_args.args[0], first)

    def test_word_whose_retries_keep_failing_is_not_retried_again(self):
        # „Jot” (J. z GTA VI): lektor mówi dobrze, rozpoznawanie słyszy co innego — 32 nieudane powtórki w logu
        text = "Jot, chodźcie tu szybciutko!"
        score = Counter()
        for _ in range(ge.LOST_RETRY_SKIP_AFTER):
            lektor, cloud, _first, _second = self._lektor(["chodźcie tu szybciutko", "chodźcie tu szybciutko"])
            lektor._lost_score = score
            lektor._synth_cloud(cloud, text, Path("x.wav"), 1.0, 0.1)
            self.assertEqual(cloud.synth.call_count, 2)
        lektor, cloud, first, _second = self._lektor(["chodźcie tu szybciutko"])
        lektor._lost_score = score
        lektor._synth_cloud(cloud, text, Path("x.wav"), 1.0, 0.1)
        self.assertEqual(cloud.synth.call_count, 1)
        self.assertIs(lektor._write_voice.call_args.args[0], first)

    def test_word_that_sometimes_comes_back_keeps_being_retried(self):
        text = "Świetnie! Wreszcie mogę to chapnąć."
        score = Counter()
        # porażka, sukces, porażka — bilans 1, słowo dalej powtarzane
        for heard in (["Wreszcie mogę.", "Wreszcie mogę."], ["Wreszcie mogę.", "Świetnie. Wreszcie mogę."],
                      ["Wreszcie mogę.", "Wreszcie mogę."]):
            lektor, cloud, _first, _second = self._lektor(heard)
            lektor._lost_score = score
            lektor._synth_cloud(cloud, text, Path("x.wav"), 1.0, 0.1)
            self.assertEqual(cloud.synth.call_count, 2)


if __name__ == "__main__":
    unittest.main()
