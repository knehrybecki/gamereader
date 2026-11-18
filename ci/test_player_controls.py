"""Pasek sterowania odtwarzacza (YouTube/Netflix) nie blokuje ręcznie zaznaczonego obszaru napisów.

18.11: YouTube nie na pełnym ekranie — czerwone elementy strony pod filmem („Subskrybuj”, miniatury) w dolnych 28 %
okna Chrome wyglądały jak pasek postępu, więc pętla pomijała każdą klatkę przez kilka minut, a lektor czytał tylko
tłumaczenie dźwięku zamiast polskich napisów z zaznaczonego obszaru."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


def engine(lock_region):
    eng = ge.Engine.__new__(ge.Engine)
    eng.source_info = (0, 33, 1512, 874, 42, "chrome")
    eng.lock_region = lock_region
    eng.region = (203, 622, 940, 211)
    return eng


class PlayerControlsTest(unittest.TestCase):
    def test_manual_region_is_read_even_with_red_on_page(self):
        with patch.object(ge, "player_controls_visible", return_value=True):
            self.assertFalse(engine(lock_region=True)._skip_for_player_controls())

    def test_auto_band_still_skips_when_controls_visible(self):
        with patch.object(ge, "player_controls_visible", return_value=True):
            self.assertTrue(engine(lock_region=False)._skip_for_player_controls())

    def test_non_browser_source_never_checks(self):
        eng = engine(lock_region=False)
        eng.source_info = (0, 0, 960, 568, 7, "ps")
        with patch.object(ge, "player_controls_visible", side_effect=AssertionError("PS5 nie ma paska YouTube")):
            self.assertFalse(eng._skip_for_player_controls())


if __name__ == "__main__":
    unittest.main()
