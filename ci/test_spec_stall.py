"""Log 20–21.11: 35 kwestii czekało na start dokładnie 4,0 s („GOTOWE 4.01s”), a za nimi cała rozmowa
szła 4–6 s za napisami. Lektor czekał na syntezę „na zapas” wariantu tej samej kwestii, a OCR podsyłał
kolejne warianty tego samego napisu („Udai się do serwisu łodzi.” / „…fodzi.” / „…todzi.”) — każdy zajmował
syntezę od nowa, więc czekanie kończył dopiero limit czasu."""
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine, ocr_variants

CLAIMED = "Udaj się do serwisu łodzi."
VARIANTS = [
    "Udai się do serwisu łodzi.",
    "Udai się do serwisu fodzi.",
    "Udai się do serwisu todzi.",
    "Udaj się do serwisu łodzi.",
]
SYNTH_SEC = 0.3
# każdy odczyt OCR inny (jak w grze na ruchomym tle) — żaden nie leży jeszcze w gotowych nagraniach
FLICKER = [f"{head} się do serwisu {tail}{end}" for end in (".", "..", "!")
           for head in ("Udai", "Udaj", "Udał") for tail in ("łodzi", "fodzi", "todzi", "lodzi")]


def spec_engine():
    engine = Engine.__new__(Engine)
    engine._spec_event = threading.Event()
    engine._spec_done = threading.Condition()
    engine._spec_busy = None
    engine._spec_item = None
    engine._ready_lock = threading.Lock()
    engine._ready = {}
    engine.speaking_text = ""
    engine._speaking_parts = []
    engine._recently_spoken = lambda *_: False
    engine._timing = lambda *_: None
    engine.lektor_volume = 80
    engine._plan_line = lambda src, translate, parts=None: (src, [(src, "calm")], 0.0, 1.0)
    engine.synths = []

    def prepare(text, **_):
        engine.synths.append(text)
        time.sleep(SYNTH_SEC)
        return "/tmp/x.wav"

    engine.lektor = SimpleNamespace(model=object(), prepare=prepare)
    return engine


class SpecStallTest(unittest.TestCase):
    def test_flicker_reads_are_one_line(self):
        for text in FLICKER:
            self.assertTrue(ocr_variants(text, CLAIMED), text)

    def test_lektor_waits_only_for_the_synthesis_in_flight(self):
        engine = spec_engine()
        threading.Thread(target=engine._spec_loop, daemon=True).start()
        stop = threading.Event()

        def ocr_flicker():
            for text in FLICKER:
                if stop.is_set():
                    return
                engine._speculate(text, False)
                time.sleep(0.05)

        feeder = threading.Thread(target=ocr_flicker, daemon=True)
        feeder.start()
        # jak w aplikacji pełnej wątków: wątek syntezy po skończonym wariancie od razu bierze następny,
        # zanim czekający lektor dostanie GIL i sprawdzi warunek
        switch = sys.getswitchinterval()
        sys.setswitchinterval(1.0)
        try:
            time.sleep(0.15)  # synteza wariantu już trwa
            engine._speaking_parts = [CLAIMED]  # lektor wziął kwestię z kolejki
            t0 = time.monotonic()
            engine._await_spec(CLAIMED)
            waited = time.monotonic() - t0
        finally:
            sys.setswitchinterval(switch)
            stop.set()
            feeder.join()
        self.assertLess(waited, SYNTH_SEC + 0.5, f"lektor czekał {waited:.2f}s na kolejne warianty OCR")

    def test_variants_of_the_line_being_read_are_not_synthesized_again(self):
        engine = spec_engine()
        engine._speaking_parts = [CLAIMED]
        threading.Thread(target=engine._spec_loop, daemon=True).start()
        engine._speculate(VARIANTS[1], False)
        time.sleep(0.1)
        engine._speculate("Skąd wzięłaś to auto? Fajne.", False)
        time.sleep(SYNTH_SEC + 0.2)
        self.assertEqual(engine.synths, ["Skąd wzięłaś to auto? Fajne."])

    def test_idle_synthesis_does_not_block(self):
        engine = spec_engine()
        t0 = time.monotonic()
        engine._await_spec(CLAIMED)
        self.assertLess(time.monotonic() - t0, 0.05)


class SpeculateCandidateTest(unittest.TestCase):
    def engine(self):
        engine = Engine.__new__(Engine)
        engine.brain = None
        engine.speaking_text = ""
        engine._speaking_parts = [CLAIMED]
        engine._spoken_texts = {}
        engine._seen_at = {}
        engine._speculate = Mock()
        return engine

    def test_new_ocr_variant_of_claimed_line_is_not_speculated(self):
        engine = self.engine()
        engine._speculate_candidate(VARIANTS[2], fresh=True)
        engine._speculate.assert_not_called()

    def test_next_line_is_still_speculated(self):
        engine = self.engine()
        engine._speculate_candidate("Skąd wzięłaś to auto? Fajne.", fresh=True)
        engine._speculate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
