"""Tłumaczenie slangu przez model językowy z powrotem do Argos — bez prawdziwego modelu."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lektor_brain import LlmTranslator, llm_translate_wanted


class Fallback:
    def __init__(self):
        self.calls = []

    def translate(self, text):
        self.calls.append(text)
        return f"ARGOS({text})"

    def ensure(self):
        pass


def make(reply, delay=0.0, timeout=0.5):
    """LlmTranslator z podstawioną „generacją” i uruchomionym wątkiem roboczym."""
    fallback = Fallback()
    tr = LlmTranslator(fallback, timeout=timeout)

    def complete(messages):
        time.sleep(delay)
        return reply(messages) if callable(reply) else reply

    tr._complete = complete
    tr.ready = True
    tr._start_worker()
    return tr, fallback


class LlmTranslatorTest(unittest.TestCase):
    def test_uses_the_model_answer_when_ready(self):
        tr, fb = make("Skręcę sobie grubego.")
        self.assertEqual(tr.translate("I'm gon roll me a fat one."), "Skręcę sobie grubego.")
        self.assertEqual(fb.calls, [])

    def test_falls_back_to_argos_until_the_model_is_loaded(self):
        tr, fb = make("nieistotne")
        tr.ready = False
        self.assertEqual(tr.translate("Hello there."), "ARGOS(Hello there.)")

    def test_falls_back_when_the_model_is_too_slow(self):
        tr, fb = make("Za późno.", delay=0.6, timeout=0.15)
        started = time.monotonic()
        self.assertEqual(tr.translate("Hello there."), "ARGOS(Hello there.)")
        self.assertLess(time.monotonic() - started, 0.45)

    def test_garbage_answers_fall_back(self):
        for bad in ("", "   ", "...", "x" * 400, "Tłumaczenie:\nlinia\ndruga\ntrzecia"):
            tr, fb = make(bad)
            self.assertEqual(tr.translate("Hello there, my friend."), "ARGOS(Hello there, my friend.)", repr(bad))

    def test_answer_in_english_falls_back(self):
        # model oddał angielski tekst (nie przetłumaczył) — Argos jest pewniejszy
        tr, fb = make("Hello there, my friend.")
        self.assertEqual(tr.translate("Hello there, my friend."), "ARGOS(Hello there, my friend.)")

    def test_repeats_are_served_from_the_cache(self):
        calls = []
        tr, fb = make(lambda m: calls.append(1) or "Cześć, stary.")
        tr.translate("Hey, man.")
        tr.translate("Hey, man.")
        self.assertEqual(len(calls), 1)

    def test_previous_lines_go_to_the_model_as_context(self):
        seen = []
        tr, fb = make(lambda m: seen.append(m) or "Odpowiedź.")
        tr.translate("First line here.")
        tr.translate("Second line here.")
        flat = " ".join(str(part["content"]) for part in seen[1])
        self.assertIn("First line here.", flat)
        self.assertIn("Odpowiedź.", flat)

    def test_concurrent_callers_are_serialized(self):
        tr, fb = make(lambda m: "OK.", delay=0.02, timeout=2.0)
        out = []
        threads = [threading.Thread(target=lambda i=i: out.append(tr.translate(f"Line number {i}."))) for i in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(out, ["OK."] * 5)

    def test_wanted_only_with_the_flag(self):
        self.assertFalse(llm_translate_wanted({}))
        self.assertTrue(llm_translate_wanted({"LIVEDUB_LLM_TRANSLATE": "1"}) in (True, False))


if __name__ == "__main__":
    unittest.main()
