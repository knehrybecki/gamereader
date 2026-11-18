"""Krótkie kwestie lektora VoiceStudio nie są sztucznie wydłużane.

Estymator długości OmniVoice wydłuża wszystko poniżej 50 tokenów (2 s przy 25 tokenach/s) krzywą z pierwiastkiem
3. stopnia: „Spoko.” trwało 1,15 s mowy zamiast ~0,5 s i w szybkim dialogu GTA lektor nie nadążał (18.11).
Próg 35 / krzywa 2,5: 8 krótkich kwestii 10,95 s → 8,38 s, błędy słów bez zmian (1/16; próg 20/2: 5/16)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import omnivoice_sidecar as sc


class FakeEstimator:
    def __init__(self):
        self.calls = []

    def estimate_duration(self, target_text, ref_text, ref_duration, low_threshold=50, boost_strength=3):
        self.calls.append((low_threshold, boost_strength))
        return 1.0


class FakeModel:
    def __init__(self, estimator):
        self.duration_estimator = estimator


class ShortLinesTest(unittest.TestCase):
    def test_short_lines_use_gentler_boost(self):
        est = FakeEstimator()
        sc.gentler_short_lines(FakeModel(est))
        est.estimate_duration("Spoko.", "Ref.", 100)
        self.assertEqual(est.calls, [(sc.SHORT_LINE_LOW_TOKENS, sc.SHORT_LINE_BOOST)])
        self.assertEqual((sc.SHORT_LINE_LOW_TOKENS, sc.SHORT_LINE_BOOST), (35, 2.5))

    def test_model_without_estimator_is_left_alone(self):
        sc.gentler_short_lines(object())  # inna wersja VoiceStudio — bez błędu


if __name__ == "__main__":
    unittest.main()
