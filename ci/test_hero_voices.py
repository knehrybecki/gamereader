"""Głosy Jasona i Lucii: gotowe próbki (GameReader/heroes/<imię>/sample.wav), rozpoznanie po imieniu w napisie
albo po barwie głosu z gry; każda niepewność — lektor (projekt docs/superpowers/specs/2026-11-18-…)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge

JASON = np.array([1.0, 0.0, 0.0], dtype=np.float32)
LUCIA = np.array([0.0, 1.0, 0.0], dtype=np.float32)
STRANGER = np.array([0.2, 0.0, 0.98], dtype=np.float32)  # cos do Jasona ~0,2 (jak strażnik w zwiastunie)


class FakeCloud:
    """Proces VoiceStudio: wektor mówcy z nagrania (pierwsza próbka = kto mówi) i z pliku próbki."""

    ready = True

    def __init__(self):
        self.registered = []

    def embed(self, audio):
        return {1: JASON, 2: LUCIA}.get(int(round(float(audio[0]) * 10)), STRANGER)

    def embed_file(self, path):
        return {"jason": JASON, "lucia": LUCIA}[Path(path).parent.name]

    def set_voice_file(self, key, path, text):
        self.registered.append((key, Path(path).name, text))
        return 0.4


def speech(who, seconds=3.0):
    """Nagranie 16 kHz, którego pierwsza próbka mówi atrapie, kto mówi (0,1 Jason, 0,2 Lucia, 0,9 ktoś)."""
    audio = np.full(int(ge.SAMPLE_RATE * seconds), 0.05, dtype=np.float32)
    audio[0] = {"jason": 0.1, "lucia": 0.2}.get(who, 0.9)
    return audio


def heroes_dir(*names):
    tmp = tempfile.TemporaryDirectory()
    for name in names:
        (Path(tmp.name) / name).mkdir()
        (Path(tmp.name) / name / "sample.wav").write_bytes(b"RIFF")
        (Path(tmp.name) / name / "sample.txt").write_text(f"I'm {name}.", encoding="utf-8")
    return tmp


class HeroBankTest(unittest.TestCase):
    def bank(self, *names):
        self.tmp = heroes_dir(*names)
        self.addCleanup(self.tmp.cleanup)
        self.cloud = FakeCloud()
        bank = ge.HeroBank(lambda: self.cloud, heroes_dir=self.tmp.name)
        self.addCleanup(bank.close)
        return bank

    def test_heroes_enrolled_from_samples(self):
        bank = self.bank("jason", "lucia")
        bank._handle(speech("jason"), 1.0, 4.0, False)
        self.assertEqual(sorted(bank.speakers), ["hero_jason", "hero_lucia"])
        self.assertTrue(all(sp["ready"] for sp in bank.speakers.values()))
        self.assertIn(("hero_jason", "sample.wav", "I'm jason."), self.cloud.registered)

    def test_close_voice_gets_hero_far_voice_gets_none(self):
        bank = self.bank("jason", "lucia")
        bank._handle(speech("jason"), 1.0, 4.0, False)
        key, sim = bank._assign(JASON, 3.0, create=True)
        self.assertEqual(key, "hero_jason")
        self.assertGreaterEqual(sim, ge.HERO_MIN_SIM)
        self.assertIsNone(bank._assign(STRANGER, 3.0, create=True)[0])
        self.assertEqual(len(bank.speakers), 2)  # nikogo nowego nie zakłada

    def test_missing_hero_folder_is_fine(self):
        bank = self.bank("jason")
        bank._handle(speech("jason"), 1.0, 4.0, False)
        self.assertEqual(list(bank.speakers), ["hero_jason"])
        empty = ge.HeroBank(lambda: FakeCloud(), heroes_dir=Path(self.tmp.name) / "nie-ma")
        self.addCleanup(empty.close)
        empty._handle(speech("jason"), 1.0, 4.0, False)
        self.assertFalse(empty.any_ready())

    def test_new_voicestudio_process_reenrolls(self):
        bank = self.bank("jason")
        bank._handle(speech("jason"), 1.0, 4.0, False)
        old = self.cloud
        self.cloud = FakeCloud()  # inny voicepack = nowy proces VoiceStudio bez głosów bohaterów
        bank.reset()
        bank._handle(speech("jason"), 5.0, 8.0, False)
        self.assertEqual([r[0] for r in self.cloud.registered], ["hero_jason"])
        self.assertEqual(len(old.registered), 1)

    def test_no_transcription_and_no_sample_collection(self):
        bank = self.bank("jason")
        bank._handle(speech("jason"), 1.0, 4.0, False)
        bank._handle(speech("stranger"), 4.0, 7.0, False)
        self.assertEqual([e["text"] for e in bank.timeline], ["", ""])  # bez transkrypcji
        self.assertEqual(bank.speakers["hero_jason"]["pool"], [])  # bez próbek z gry
        self.assertEqual([r[0] for r in self.cloud.registered], ["hero_jason"])  # jedyny głos: próbka ze zwiastuna

    def test_voice_for_returns_hero_speaking_when_subtitle_appeared(self):
        bank = self.bank("jason", "lucia")
        bank._backend = self.cloud
        now = time.monotonic()
        bank._handle(speech("lucia"), now - 2.0, now + 1.0, False)
        self.assertEqual(bank.voice_for(now - 1.5, wait=0.1), "hero_lucia")
        bank._handle(speech("stranger"), now + 2.0, now + 4.0, False)
        self.assertIsNone(bank.voice_for(now + 2.5, wait=0.1))


if __name__ == "__main__":
    unittest.main()
