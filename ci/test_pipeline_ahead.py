"""Nagranie 18.11 07:16 (GTA VI na YouTube): kolejna kwestia czekała na koniec poprzedniej, zanim ruszyła
synteza (+0,7 s do każdego wejścia), a napisy z kadru („DA VES”, „LAY CHEE!”) lektor czytał na głos."""
import sys
import unittest
from unittest.mock import Mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine, scene_text_junk


class PipelineAheadTest(unittest.TestCase):
    def _engine(self):
        engine = Engine.__new__(Engine)
        engine.brain = None
        engine._is_current = lambda *_: False
        engine._recently_spoken = lambda *_: False
        engine.no_barge_in = True
        engine._enqueue = Mock()
        engine._speculate = Mock()
        engine.speaking_text = ""
        return engine

    def test_line_arriving_during_speech_is_synthesized_right_away(self):
        engine = self._engine()
        engine.speaking_text = "Czyli jesteśmy w ekipie?"
        engine._offer_line("Chyba tak.", False)
        engine._enqueue.assert_called_once()
        engine._speculate.assert_called_once_with("Chyba tak.", False)

    def test_idle_lektor_takes_the_line_itself(self):
        engine = self._engine()
        engine._offer_line("Chyba tak.", False)
        engine._enqueue.assert_called_once()
        engine._speculate.assert_not_called()


class SceneTextJunkTest(unittest.TestCase):
    def test_signs_and_stickers_from_the_frame_are_junk(self):
        for text in ("DA VES", "FLO C", "LAY CHEE!", "ALI", "LPRAV CHELSE C", ": / :łął"):
            self.assertTrue(scene_text_junk(text), text)

    def test_dialogue_stays(self):
        for text in ("NIE!", "STÓJ!", "Chyba tak.", "O, tak!", "Znów w Bayside.", "Joł, Jay! Co słychać, skarbie?"):
            self.assertFalse(scene_text_junk(text), text)


if __name__ == "__main__":
    unittest.main()
