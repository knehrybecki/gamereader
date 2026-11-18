"""Lektor VoiceStudio ma pierwszeństwo do GPU przed modelem decyzji (Qwen na MLX).

18.11: synteza kwestii 1,5–3,6 s (osobno 0,8–1,1 s), w logu co chwilę „model decyzji wolny: ~500 ms” — oba modele
liczyły naraz na GPU, lektor nie nadążał za napisami i nadganianie ucinało zdania."""
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge
import lektor_brain as lb


class GpuPriorityTest(unittest.TestCase):
    def test_brain_waits_while_voice_synthesizes(self):
        brain = lb.LektorBrain.__new__(lb.LektorBrain)
        busy = [True, True, True, False]
        brain.yield_to = lambda: busy.pop(0) if busy else False
        t0 = time.monotonic()
        brain._wait_for_gpu()
        self.assertEqual(busy, [])  # czekał, aż synteza się skończy
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_brain_does_not_wait_forever(self):
        brain = lb.LektorBrain.__new__(lb.LektorBrain)
        brain.yield_to = lambda: True
        t0 = time.monotonic()
        brain._wait_for_gpu(max_wait=0.2)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_engine_reports_voice_synthesis_busy(self):
        eng = ge.Engine.__new__(ge.Engine)
        lock = threading.Lock()
        eng.lektor = SimpleNamespace(cloud=SimpleNamespace(lock=lock))
        self.assertFalse(eng._voice_synth_busy())
        with lock:
            self.assertTrue(eng._voice_synth_busy())
        eng.lektor = SimpleNamespace(cloud=None)  # Supertonic: CPU, bez konfliktu
        self.assertFalse(eng._voice_synth_busy())


if __name__ == "__main__":
    unittest.main()
