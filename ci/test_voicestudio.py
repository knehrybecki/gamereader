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
        return "prompt"
    def generate(self, text, language, voice_clone_prompt, num_step, speed=None):
        assert language == "pl" and voice_clone_prompt == "prompt"
        t = np.arange(int(24000 * 0.05 * len(text))) / 24000
        return [_Wav((np.sin(2 * np.pi * 150 * t) * 0.3).astype("float32"))]
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
        (packs / "lektor/voice.json").write_text('{"deepen": 0.92}')
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

    def test_missing_voicepack_falls_back_to_supertonic(self):
        backend = ge.OmniVoiceLocal("nie-ma")
        self.wait_ready(backend)
        self.assertFalse(backend.ready)
        self.assertIn("Supertonic", backend.error)
        self.assertIsNone(backend.synth("Hej."))


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

        def session(path, sess_options=None, providers=None, **kwargs):
            created["opts"] = sess_options
            created["providers"] = providers
            return "session"

        loader = types.SimpleNamespace(
            ort=types.SimpleNamespace(InferenceSession=session, SessionOptions=lambda: opts)
        )
        ge._cap_onnx_arena(loader)
        loader.ort.InferenceSession("model.onnx", sess_options=opts, providers=["CPUExecutionProvider"])
        self.assertFalse(opts.enable_cpu_mem_arena)
        self.assertFalse(opts.enable_mem_pattern)


if __name__ == "__main__":
    unittest.main()
