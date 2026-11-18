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


class ColonSpeakerTest(unittest.TestCase):
    """Imię z dwukropkiem przed kwestią („JASON: …”) — pewny sygnał, kto mówi; lektor i tak go nie czyta."""

    def test_name_remembered_when_cut(self):
        self.assertEqual(ge.strip_colon_speaker("JASON: Wsiadaj do auta."), "Wsiadaj do auta.")
        self.assertEqual(ge.colon_speaker("Wsiadaj do auta."), "JASON")
        self.assertEqual(ge.colon_speaker("wsiadaj do auta"), "JASON")  # po normalizacji OCR (wielkość, kropka)
        self.assertIsNone(ge.colon_speaker("Coś zupełnie innego."))

    def test_hero_key_by_name(self):
        tmp = heroes_dir("jason", "lucia")
        self.addCleanup(tmp.cleanup)
        bank = ge.HeroBank(lambda: FakeCloud(), heroes_dir=tmp.name)
        self.addCleanup(bank.close)
        bank._handle(speech("jason"), 1.0, 4.0, False)
        self.assertEqual(bank.key_for_name("Jason"), "hero_jason")
        self.assertEqual(bank.key_for_name("LUCIA"), "hero_lucia")
        self.assertIsNone(bank.key_for_name("LUCA"))  # inne imię = lektor, nie najbliższy bohater


class PickVoiceTest(unittest.TestCase):
    """Kto czyta kwestię: imię z napisu > barwa głosu > lektor. W trybie bohaterów nigdy cisza."""

    def engine(self, bank, cfg=None):
        from types import SimpleNamespace

        eng = ge.Engine.__new__(ge.Engine)
        eng.speakers = bank
        eng.lektor = SimpleNamespace(cloud=FakeCloud())
        eng.cfg = dict(cfg or {})
        eng._seen_at = {}
        eng._timing = lambda *_a: None
        eng.emit = lambda *_a: None
        return eng

    def hero_bank(self, voice=None):
        tmp = heroes_dir("jason", "lucia")
        self.addCleanup(tmp.cleanup)
        bank = ge.HeroBank(lambda: FakeCloud(), heroes_dir=tmp.name)
        self.addCleanup(bank.close)
        bank._handle(speech("jason"), 1.0, 4.0, False)
        bank.any_ready = lambda: True
        self.calls = []

        def voice_for(t, wait=0.0, text=None):
            self.calls.append((wait, text))
            return voice

        bank.voice_for = voice_for
        return bank

    def test_name_wins_over_voice(self):
        eng = self.engine(self.hero_bank(voice="hero_lucia"))
        ge.strip_colon_speaker("JASON: Uciekamy stąd, szybko.")
        eng._seen_at["Uciekamy stąd, szybko."] = time.monotonic()
        self.assertEqual(eng._pick_voice(["Uciekamy stąd, szybko."], "Uciekamy stąd, szybko.", False), ("hero_jason", False))
        self.assertEqual(self.calls, [])

    def test_name_in_one_part_of_line_decides(self):
        eng = self.engine(self.hero_bank(voice=None))
        ge.strip_colon_speaker("LUCIA: Gdzie byłeś całą noc?")
        parts = ["Hej.", "Gdzie byłeś całą noc?"]
        self.assertEqual(eng._pick_voice(parts, " ".join(parts), False), ("hero_lucia", False))

    def test_other_name_means_lektor(self):
        eng = self.engine(self.hero_bank(voice="hero_jason"))
        ge.strip_colon_speaker("LUCA: Don Torrisi czeka na nas.")
        self.assertEqual(eng._pick_voice(["Don Torrisi czeka na nas."], "Don Torrisi czeka na nas.", False), (None, False))

    def test_voice_match_used_without_name(self):
        eng = self.engine(self.hero_bank(voice="hero_lucia"))
        eng._seen_at["Musimy porozmawiać."] = time.monotonic()
        self.assertEqual(eng._pick_voice(["Musimy porozmawiać."], "Musimy porozmawiać.", False), ("hero_lucia", False))
        self.assertEqual(self.calls, [(ge.HERO_WAIT_SEC, None)])

    def test_unsure_means_lektor_never_silence(self):
        eng = self.engine(self.hero_bank(voice=None), cfg={"cloneOnly": True})
        eng._seen_at["Kto tam?"] = time.monotonic()
        self.assertEqual(eng._pick_voice(["Kto tam?"], "Kto tam?", False), (None, False))

    def test_old_clone_mode_unchanged(self):
        bank = ge.SpeakerBank(lambda: FakeCloud(), lambda _a: "")
        self.addCleanup(bank.close)
        eng = self.engine(bank, cfg={"cloneOnly": True})
        self.assertEqual(eng._pick_voice(["Kto tam?"], "Kto tam?", False), (None, True))

    def test_hero_mode_wins_over_clone_mode(self):
        from types import SimpleNamespace

        eng = ge.Engine.__new__(ge.Engine)
        eng.speakers = None
        eng.lektor = SimpleNamespace(cloud=None)
        eng.clone_voices, eng.hero_voices = True, True
        eng._rebuild_speakers()
        self.addCleanup(eng.speakers.close)
        self.assertIsInstance(eng.speakers, ge.HeroBank)
        eng.hero_voices = False
        eng._rebuild_speakers()
        self.assertIs(type(eng.speakers), ge.SpeakerBank)
        eng.clone_voices = False
        eng._rebuild_speakers()
        self.assertIsNone(eng.speakers)


class ReviewFixesTest(unittest.TestCase):
    """Poprawki po recenzji całej gałęzi (18.11)."""

    def test_short_named_line_does_not_tag_other_lines(self):
        ge.strip_colon_speaker("LUCIA: Co?")
        self.assertIsNone(ge.colon_speaker("Musimy stąd uciekać, coś się dzieje."))
        ge.strip_colon_speaker("JASON: Tak.")
        self.assertIsNone(ge.colon_speaker("Nie wiem, jak to zrobić, ale takie jest życie."))

    def test_name_expires(self):
        from unittest.mock import patch

        ge.strip_colon_speaker("JASON: Dalej, ruszaj się!")
        later = time.monotonic() + ge.SPEAKER_MATCH_TTL + 1.0
        with patch.object(ge.time, "monotonic", return_value=later):
            self.assertIsNone(ge.colon_speaker("Dalej, ruszaj się!"))  # NPC minutę później — nie Jason

    def test_hero_voice_tempo_does_not_follow_lektor_pack(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "glos").mkdir()
        (Path(tmp.name) / "glos/voice.json").write_text('{"lps": 8.56}')
        from unittest.mock import patch

        with patch.object(ge, "VOICEPACK_DIR", Path(tmp.name)):
            ov = ge.OmniVoiceLocal("glos")
        self.assertAlmostEqual(ov.job_speed(1.0, voice="hero_jason"), ge.OMNI_SPEED_SCALE)
        self.assertNotAlmostEqual(ov.job_speed(1.0), ge.OMNI_SPEED_SCALE)  # lektor dalej kalibrowany

    def test_voice_miss_is_logged(self):
        logs = []
        pv = PickVoiceTest()
        tmp = heroes_dir("jason")
        self.addCleanup(tmp.cleanup)
        bank = ge.HeroBank(lambda: FakeCloud(), heroes_dir=tmp.name)
        self.addCleanup(bank.close)
        bank._handle(speech("jason"), 1.0, 4.0, False)
        bank.any_ready = lambda: True
        bank.voice_for = lambda t, wait=0.0, text=None: None
        eng = pv.engine(bank)
        eng._timing = logs.append
        eng._seen_at["Kto tam?"] = time.monotonic()
        eng._pick_voice(["Kto tam?"], "Kto tam?", False)
        self.assertTrue(any("lektor" in line for line in logs), logs)


class HeroWaitTest(unittest.TestCase):
    def test_line_waits_briefly_for_hero_voice(self):
        # 18.11: przy 0,8 s czekania lektor nie nadążał w dialogach (od napisu ~0,7 s przed syntezą)
        self.assertLessEqual(ge.HERO_WAIT_SEC, 0.3)


if __name__ == "__main__":
    unittest.main()
