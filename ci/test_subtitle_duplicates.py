"""Regresje powtórek z logu sceny dialogowej 12:21."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine, same_utterance, _join_parts

VARIANTS = [
    'Jest bezdomny: mie ma dokąd pojc!',
    'Jeev bezdomny: Nie ma dokąd pójść!',
    'Jest bezdomny. Ne ma dekad posr!',
]


class StopLoop(BaseException):
    pass


class DuplicateTest(unittest.TestCase):
    def engine(self):
        engine = Engine.__new__(Engine)
        engine._spoken_folds = {}
        engine.speak_cooldown = 5.5
        engine._brain_junk = lambda *_: False
        return engine

    def test_logged_variants_are_one_utterance(self):
        for a in VARIANTS:
            for b in VARIANTS:
                self.assertTrue(same_utterance(a, b), (a, b))

    def test_batch_does_not_repeat_ocr_variants(self):
        engine = self.engine()
        new = 'Nic mnie to nie obchodzi. O Boże!'
        self.assertEqual(engine._fresh_parts(VARIANTS + [new]), [VARIANTS[0], new])

    def test_spoken_variant_blocks_later_variants(self):
        engine = self.engine()
        engine._mark_spoken(VARIANTS[1])
        self.assertEqual(engine._fresh_parts(VARIANTS), [])

    def test_real_new_dialogue_is_kept(self):
        engine = self.engine()
        engine._mark_spoken(VARIANTS[1])
        text = 'Jest bezdomny. Nie ma własnego samochodu!'
        self.assertEqual(engine._fresh_parts([text]), [text])

    def test_playback_rebuilds_text_after_removing_spoken_part(self):
        engine = self.engine()
        old = 'Zatrzymaj samochód przed sklepem.'
        remaining = ['Idę kupić coś do jedzenia.', 'Poczekaj tutaj na mnie.']
        engine._mark_spoken(old)
        engine.has_pending = threading.Event()
        engine.has_pending.set()
        engine._take_pending = lambda: (_join_parts([old] + remaining), False, remaining[-1], [old] + remaining)
        engine.speaking_text = ''
        engine._ready_lock = threading.Lock()
        engine._ready = {}
        engine._plan_line = Mock(side_effect=StopLoop)
        engine.lektor = SimpleNamespace(busy_until=0)
        engine._tts_interrupt = threading.Event()
        engine._duck_release = lambda: None
        with self.assertRaises(StopLoop):
            engine._tts_loop()
        engine._plan_line.assert_called_once_with(_join_parts(remaining), False, remaining)


if __name__ == '__main__':
    unittest.main()
