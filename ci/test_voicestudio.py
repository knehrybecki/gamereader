"""Lektor z VoiceStudio (voicepack) — prawdziwy proces w tle, atrapa modelu OmniVoice."""
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge

FAKE_TORCH = """
class _Mps:
    @staticmethod
    def is_available():
        return False
class backends:
    mps = _Mps()
float16 = float32 = None
"""
FAKE_MODEL = """
import numpy as np
class _Wav:
    def __init__(self, a): self.a = a
    def squeeze(self): return self
    def float(self): return self
    def cpu(self): return self
    def numpy(self): return self.a
class OmniVoice:
    @classmethod
    def from_pretrained(cls, *a, **k): return cls()
    def create_voice_clone_prompt(self, ref, text):
        assert text, "brak tekstu voicepacka"
        return "default" if ref.endswith("/voice.wav") else "clone"
    def generate(self, text, language, voice_clone_prompt, num_step, speed=None, guidance_scale=2.0):
        assert language in ("pl", "es") and voice_clone_prompt in ("default", "clone")
        t = np.arange(int(24000 * 0.05 * len(text))) / 24000
        freq = (150 if voice_clone_prompt == "default" else 300) + (50 if language == "es" else 0)
        freq += 25 if guidance_scale != 2.0 else 0  # atrapa: inna siła dopasowania = inny ton
        freq += 40 if num_step == 8 else 0  # atrapa: więcej kroków (jakość wysoka) = inny ton
        return [_Wav((np.sin(2 * np.pi * freq * t) * 0.3).astype("float32"))]
"""


@unittest.skipIf(sys.platform == "win32", "VoiceStudio tylko na Macu")
class VoiceStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "VoiceStudio"
        (root / ".venv/bin").mkdir(parents=True)
        # skrypt, nie symlink — symlink do Pythona z venv gubi jego pakiety
        wrapper = root / ".venv/bin/python"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        wrapper.chmod(0o755)
        (root / "torch").mkdir()
        (root / "torch/__init__.py").write_text(FAKE_TORCH)
        (root / "omnivoice/models").mkdir(parents=True)
        (root / "omnivoice/__init__.py").write_text("")
        (root / "omnivoice/models/__init__.py").write_text("")
        (root / "omnivoice/models/omnivoice.py").write_text(textwrap.dedent(FAKE_MODEL))
        packs = Path(self.tmp.name) / "voicepacks"
        (packs / "lektor").mkdir(parents=True)
        (packs / "lektor/voice.wav").write_bytes(b"RIFF")
        (packs / "lektor/voice.txt").write_text("Spokojny głos lektora.", encoding="utf-8")
        (packs / "lektor/voice.json").write_text('{"deepen": 0.92, "styles": {"zywy": {"lps": 11.0}}}')
        (packs / "lektor/voice-zywy.wav").write_bytes(b"RIFF")
        (packs / "lektor/voice-zywy.txt").write_text("Żywy głos lektora!", encoding="utf-8")
        self.patches = [
            patch.object(ge, "VOICESTUDIO_ROOT", root),
            patch.object(ge, "VOICEPACK_DIR", packs),
            patch.object(ge, "CACHE_DIR", Path(self.tmp.name) / "cache"),
            patch.object(ge, "log_timing", lambda *_: None),  # nie do prawdziwego logu lektora
        ]
        for p in self.patches:
            p.start()
        (Path(self.tmp.name) / "cache").mkdir()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def wait_ready(self, backend):
        deadline = time.monotonic() + 20
        while not backend.ready and backend.failed_until == 0.0 and time.monotonic() < deadline:
            time.sleep(0.05)

    def test_voicepack_reads_lines_in_background_process(self):
        self.assertEqual(ge.voicepacks(), ["lektor"])
        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            self.assertTrue(backend.ready, backend.error)
            self.assertIn("asetrate", backend.pre_filter)  # voicepack „pogrubiony”
            audio = backend.synth("Złap Franklina.", speed=1.0)
            self.assertIsNotNone(audio)
            self.assertGreater(audio.size, 1000)
        finally:
            backend.close()

    def test_hero_voice_from_sample_file(self):
        import numpy as np

        sample = Path(self.tmp.name) / "jason.wav"
        sample.write_bytes(b"RIFF")
        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            default = backend.synth("Wsiadaj.")
            self.assertIsNotNone(backend.set_voice_file("hero_jason", sample, "I'm here for Lucia."))
            self.assertFalse(np.array_equal(default, backend.synth("Wsiadaj.", voice="hero_jason")))
            self.assertTrue(sample.exists())  # próbka bohatera zostaje (to nie plik tymczasowy)
        finally:
            backend.close()

    def test_voicepack_guidance_reaches_model(self):
        import json
        import numpy as np

        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            plain = backend.synth("Dobrze wam poszło.")
        finally:
            backend.close()
        meta = Path(self.tmp.name) / "voicepacks/lektor/voice.json"
        meta.write_text(json.dumps({**json.loads(meta.read_text()), "guidance": 3.0}))
        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            self.assertFalse(np.array_equal(plain, backend.synth("Dobrze wam poszło.")))
        finally:
            backend.close()

    def test_high_quality_steps_reach_model(self):
        import numpy as np

        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            fast_tag = backend.tag
            fast = backend.synth("Dobrze wam poszło.")
            backend.steps = 8  # „Jakość głosu: Wysoka” — bez przeładowania modelu
            self.assertNotEqual(fast_tag, backend.tag)  # cache nie poda nagrania z 6 kroków
            self.assertFalse(np.array_equal(fast, backend.synth("Dobrze wam poszło.")))
        finally:
            backend.close()

    def test_lively_register_uses_its_own_sample(self):
        import numpy as np

        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            calm = backend.synth("Uciekaj stąd.")
            lively = backend.synth("Uciekaj stąd.", style="zywy")
            self.assertFalse(np.array_equal(calm, lively))  # atrapa modelu: inna próbka = inny ton
            self.assertTrue(np.array_equal(calm, backend.synth("Uciekaj stąd.", style="nie-ma")))  # brak rejestru = zwykły
        finally:
            backend.close()

    def test_spanish_sentence_reaches_model_as_spanish(self):
        import numpy as np

        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            polish = backend.synth("Hola amigo.")
            spanish = backend.synth("Hola amigo.", language="es")
            self.assertFalse(np.array_equal(polish, spanish))  # atrapa modelu: „es” = inny ton
        finally:
            backend.close()

    def test_missing_voicepack_falls_back_to_supertonic(self):
        backend = ge.OmniVoiceLocal("nie-ma")
        self.wait_ready(backend)
        self.assertFalse(backend.ready)
        self.assertIn("Supertonic", backend.error)
        self.assertIsNone(backend.synth("Hej."))

    def test_registered_character_changes_voice_and_sample_is_removed(self):
        import numpy as np

        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            default = backend.synth("Cześć.")
            self.assertIsNotNone(backend.set_voice("s1", np.zeros(16000, dtype=np.float32), "Hello."))
            clone = backend.synth("Cześć.", voice="s1")
            self.assertFalse(np.array_equal(default, clone))
            self.assertIsNone(backend.synth("Cześć.", voice="unknown"))  # brak klonu: kwestia przechodzi na Supertonic, a nie na obcy głos
            self.assertEqual(list((ge.CACHE_DIR / "speakers").glob("*.wav")), [])
        finally:
            backend.close()

    def test_wrong_reply_id_stops_process_instead_of_playing_wrong_audio(self):
        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            backend.proc.stdin.write(b'{"id": -1, "op": "unknown"}\n')
            backend.proc.stdin.flush()
            time.sleep(0.1)
            self.assertIsNone(backend.synth("Cześć."))
            self.assertFalse(backend.ready)
            self.assertIsNone(backend.proc)
        finally:
            backend.close()

    def test_timeout_closes_process_and_cleans_reference(self):
        import numpy as np

        backend = ge.OmniVoiceLocal("lektor")
        try:
            self.wait_ready(backend)
            with patch("select.select", return_value=([], [], [])):
                self.assertIsNone(backend.set_voice("s1", np.zeros(16000, dtype=np.float32), "Hello."))
            self.assertFalse(backend.ready)
            self.assertIsNone(backend.proc)
            self.assertEqual(list((ge.CACHE_DIR / "speakers").glob("*.wav")), [])
        finally:
            backend.close()


class MemoryCapTest(unittest.TestCase):
    def test_mlx_cache_is_capped_before_models_grow(self):
        import types
        import lektor_brain

        seen = {}

        def set_memory_limit(n):
            seen["memory"] = n
            return 0

        def set_cache_limit(n):
            seen["cache"] = n
            return 0

        def clear_cache():
            seen["cleared"] = True

        mx = types.SimpleNamespace(
            set_memory_limit=set_memory_limit,
            set_cache_limit=set_cache_limit,
            clear_cache=clear_cache,
        )
        with patch.dict(sys.modules, {"mlx": types.SimpleNamespace(core=mx), "mlx.core": mx}):
            lektor_brain.cap_mlx_cache()
        self.assertEqual(seen["cache"], lektor_brain.BRAIN_MLX_CACHE_BYTES)
        self.assertLessEqual(seen["memory"], 12 * 1024**3)
        self.assertTrue(seen["cleared"])

    def test_onnx_sessions_do_not_keep_a_growing_arena(self):
        import types

        opts = types.SimpleNamespace(enable_cpu_mem_arena=True, enable_mem_pattern=True)
        created = {}

        class Session:
            def __init__(self, path, sess_options=None, providers=None, **kwargs):
                created["opts"] = sess_options
                created["providers"] = providers

        loader = types.SimpleNamespace(
            ort=types.SimpleNamespace(InferenceSession=Session, SessionOptions=lambda: opts)
        )
        ge._cap_onnx_arena(loader)
        made = loader.ort.InferenceSession("model.onnx", sess_options=opts, providers=["CPUExecutionProvider"])
        # Supertonic odrzuca sesję, która nie przechodzi isinstance(…, ort.InferenceSession)
        self.assertIsInstance(made, loader.ort.InferenceSession)
        self.assertFalse(opts.enable_cpu_mem_arena)
        self.assertFalse(opts.enable_mem_pattern)


class VoiceStudioNamesTest(unittest.TestCase):
    """VoiceStudio (OmniVoice) mówi angielskie nazwy po angielsku z oryginalnego zapisu — spolszczenie dla Supertonic
    psuło je („Wajs S'iti” → „wajce ścici”). Lucia i imiona hiszpańskie zostają po hiszpańsku (Lus'ija)."""

    def spoken_by_voicestudio(self, text, with_language=False):
        lektor = ge.MaleLektor.__new__(ge.MaleLektor)
        lektor._cloud_prev = ("", 0.0)
        lektor.on_cloud_error = None
        lektor._write_voice = lambda *args: None
        sent = []

        class Cloud:
            rate = 24000
            error = ""

            def synth(self, text, language="pl", **kwargs):
                sent.append((text, language))
                return ge.np.zeros(2400, dtype=ge.np.float32)

        lektor._synth_cloud(Cloud(), text, "out.wav", 1.0, 0.0)
        return sent[0] if with_language else sent[0][0]

    def test_english_stays_english_lucia_spanish(self):
        spoken = self.spoken_by_voicestudio("Lucia, wsiadaj. Jason czeka w Vice City przy Ocean Beach.")
        self.assertEqual(spoken, "Lus'ija, wsiadaj. Jason czeka w Vice City przy Ocean Beach.")

    def test_declined_names(self):
        self.assertEqual(self.spoken_by_voicestudio("Lucię już złapali, Jasona szukają."),
                         "Lus'iję już złapali, Jasona szukają.")

    def test_spanish_names_keep_spanish_pronunciation(self):
        self.assertEqual(self.spoken_by_voicestudio("García mówi, że policía jedzie."),
                         ge.english_names_pl("García mówi, że policía jedzie."))

    def test_supertonic_still_respells_english(self):
        self.assertEqual(ge.english_names_pl("Jason w Vice City."), "Dżejson w Wajs S'iti.")

    def test_supertonic_only_tricks_stay_out(self):
        # „staary” to obejście Supertonic (czytał „stery”) — OmniVoice przeciągnąłby „a”
        self.assertEqual(self.spoken_by_voicestudio("Hej, stary."), "Hej, stary.")


class VoiceStudioTempoTest(unittest.TestCase):
    """Każdy głos z nagrania mówi własnym tempem (kopiuje próbkę) — voice.json „lps” = litery/s przy speed 1,0,
    a LiveDub dobiera speed tak, żeby lektor mówił jak w filmie (~11 liter/s, „Protektor” 9,8, romans 10,9)."""

    def pack(self, meta):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        packs = Path(tmp.name)
        (packs / "glos").mkdir()
        (packs / "glos/voice.json").write_text(meta, encoding="utf-8")
        p = patch.object(ge, "VOICEPACK_DIR", packs)
        p.start()
        self.addCleanup(p.stop)
        return ge.OmniVoiceLocal("glos")

    def test_measured_voice_reaches_film_tempo(self):
        ov = self.pack('{"lps": 10.4}')
        self.assertAlmostEqual(ov.job_speed(1.0), round(ge.OMNI_TARGET_LPS / 10.4, 2))
        self.assertIn(f"{ge.OMNI_TARGET_LPS / 10.4:.3f}", ov.tag)  # inne tempo = inne nagrania w cache

    def test_fast_voice_is_slowed_down(self):
        speed = self.pack('{"lps": 13.1}').job_speed(1.0)
        self.assertLess(speed, 1.0)
        self.assertAlmostEqual(speed, round(ge.OMNI_TARGET_LPS / 13.1, 2))

    def test_voice_without_measurement_keeps_old_scale(self):
        self.assertAlmostEqual(self.pack('{"deepen": 1.0}').job_speed(1.0), ge.OMNI_SPEED_SCALE)

    def test_speed_stays_in_intelligible_range(self):
        ov = self.pack('{"lps": 10.4}')
        self.assertEqual(ov.job_speed(5.0), 1.5)  # klony do 1,5 bez błędów słów (test 18.11)
        self.assertEqual(ov.job_speed(0.1), 0.7)


class SpanishLineTest(unittest.TestCase):
    """Wybór 18.11 (próbka 3): zdanie całe po hiszpańsku VoiceStudio mówi z oryginalnej pisowni modelem hiszpańskim;
    polskie zdanie z hiszpańskim wtrąceniem zostaje po polsku, Lucia dalej „Lus'ija” (próbka 5)."""

    def test_spanish_sentences(self):
        for text in ("Hola, señor García! Vámonos, la policía ya viene.", "¿Qué pasa, hermano?", "No sé.",
                     "Vamos, vamos, rápido!", "Gracias por todo, mi amor."):
            self.assertTrue(ge.spanish_sentence(text), text)

    def test_polish_sentences_with_spanish_words(self):
        for text in ("Dobra, chodźmy. Vámonos, hermano.", "Lucia, wsiadaj do auta.", "No dobra, jedziemy.",
                     "Señor García czeka na zewnątrz.", "Jason czeka w Vice City.", "To nie jest takie proste."):
            self.assertFalse(ge.spanish_sentence(text), text)

    def test_voicestudio_speaks_spanish_sentence_in_spanish(self):
        names = VoiceStudioNamesTest()
        spoken = names.spoken_by_voicestudio("Hola, señor García! Vámonos.", with_language=True)
        self.assertEqual(spoken, ("Hola, señor García! Vámonos.", "es"))

    def test_voicestudio_polish_sentence_stays_polish(self):
        names = VoiceStudioNamesTest()
        self.assertEqual(names.spoken_by_voicestudio("Lucia, wsiadaj.", with_language=True), ("Lus'ija, wsiadaj.", "pl"))


class LektorStyleTest(unittest.TestCase):
    """Melodia jak w filmie (pomiar 18.11, S.W.A.T. i „Królewska niespodzianka”): pytania, wykrzyknienia i mocno
    przeżywane kwestie lektor mówi wyżej — czytamy je próbką „żywą”, resztę spokojną."""

    def test_questions_exclamations_and_excitement_are_lively(self):
        self.assertEqual(ge.lektor_style("Naprawdę?"), "zywy")
        self.assertEqual(ge.lektor_style("Uciekaj!", mood="lively"), "zywy")
        self.assertEqual(ge.lektor_style("Dobra.", arousal=0.6), "zywy")

    def test_plain_and_soft_lines_stay_calm(self):
        self.assertIsNone(ge.lektor_style("Dobra, jedziemy do portu."))
        self.assertIsNone(ge.lektor_style("Przepraszam… nie chciałem.", mood="soft", arousal=0.6))

    def test_each_register_reaches_film_tempo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "glos").mkdir()
        (Path(tmp.name) / "glos/voice.json").write_text('{"lps": 10.18, "styles": {"zywy": {"lps": 10.98}}}')
        with patch.object(ge, "VOICEPACK_DIR", Path(tmp.name)):
            ov = ge.OmniVoiceLocal("glos")
        self.assertAlmostEqual(ov.job_speed(1.0), round(ge.OMNI_TARGET_LPS / 10.18, 2))
        self.assertAlmostEqual(ov.job_speed(1.0, "zywy"), round(ge.OMNI_TARGET_LPS / 10.98, 2))
        self.assertIn("zywy", ov.tag)  # nowy rejestr = nowe nagrania w cache

    def test_voicestudio_gets_register_for_line(self):
        names = VoiceStudioNamesTest()
        lektor = ge.MaleLektor.__new__(ge.MaleLektor)
        lektor._cloud_prev = ("", 0.0)
        lektor.on_cloud_error = None
        lektor._write_voice = lambda *args: None
        got = {}

        class Cloud:
            rate = 24000
            error = ""

            def synth(self, text, style=None, **kwargs):
                got["style"] = style
                return ge.np.zeros(2400, dtype=ge.np.float32)

        lektor._synth_cloud(Cloud(), "Naprawdę?", "out.wav", 1.0, 0.0, style="zywy")
        self.assertEqual(got["style"], "zywy")


if __name__ == "__main__":
    unittest.main()
