#!/usr/bin/env python3
"""Lektor z VoiceStudio (OmniVoice) jako proces w tle LiveDub.

Uruchamiany Pythonem z VoiceStudio: model ładuje się raz, głos domyślny to „voicepack” — wzorcowe nagranie
(voice.wav + voice.txt) wygenerowane z opisu, więc każda kwestia brzmi tym samym lektorem.

Klonowanie głosów postaci (09.10): proces umie też
- policzyć wektor mówcy z nagrania (wespeaker z cache pyannote) — silnik grupuje nimi wypowiedzi w postacie,
- zapamiętać głos postaci z próbki (create_voice_clone_prompt) i czytać nim kwestie.

Protokół (JSON w liniach, jedna odpowiedź na linię):
  stdin  {"id", "text", "speed", "out", "voice"?}                    → {"id", "ok", "error"?}   (synteza; voice = klucz postaci)
         {"op": "voice", "id", "key", "wav", "text"}                 → {"id", "ok", "secs"?}    (zapamiętaj głos postaci)
         {"op": "embed", "id", "wav"}                                → {"id", "ok", "emb": [...]} (wektor mówcy, wav 16 kHz mono)
Pierwsza linia na stdout: {"ready": true, "rate": 24000} albo {"ready": false, "error": "..."}.
"""
import json
import sys
import wave

RATE = 24000
PROTOCOL_OUT = sys.stdout


def reply(payload):
    PROTOCOL_OUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
    PROTOCOL_OUT.flush()


class Embedder:
    """Wektor mówcy: pyannote/wespeaker-voxceleb-resnet34-LM z lokalnego cache (bez sieci).

    Załadowanie na żądanie — synteza nie czeka na model mówców, który i tak jest potrzebny dopiero po kilku wypowiedziach.
    Dwie łatki tylko na czas ładowania: nowszy huggingface_hub nie zna `use_auth_token`, a nowszy PyTorch żąda
    weights_only dla oficjalnego checkpointu pyannote z własnego cache."""

    def __init__(self):
        self.inf = None
        self.failed = None

    def load(self):
        import glob
        import os

        import torch

        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import huggingface_hub as hub

        original_download, original_load = hub.hf_hub_download, torch.load
        hub.hf_hub_download = lambda *a, use_auth_token=None, **k: original_download(*a, **k)
        torch.load = lambda *a, **k: original_load(*a, **{**k, "weights_only": False})
        try:
            from pyannote.audio import Inference, Model

            from huggingface_hub.constants import HF_HUB_CACHE

            paths = sorted(glob.glob(f"{HF_HUB_CACHE}/models--pyannote--wespeaker-voxceleb-resnet34-LM/snapshots/*/pytorch_model.bin"))
            if not paths:
                raise RuntimeError("brak wespeaker w cache")
            self.inf = Inference(Model.from_pretrained(paths[0]), window="whole")
            try:
                self.inf.to(torch.device("mps" if torch.backends.mps.is_available() else "cpu"))
            except Exception:
                pass
        finally:
            hub.hf_hub_download, torch.load = original_download, original_load

    def embed(self, wav_path):
        import numpy as np
        import soundfile as sf
        import torch

        if self.failed:
            raise RuntimeError(self.failed)
        if self.inf is None:
            try:
                self.load()
            except Exception as exc:
                self.failed = str(exc)[:160]
                raise
        audio, rate = sf.read(wav_path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        vector = self.inf({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": rate})
        vector = np.asarray(vector, dtype="float32").reshape(-1)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector).all() or norm < 1e-9:
            raise ValueError("nieprawidłowy wektor mówcy")
        return (vector / norm).tolist()


def main():
    sys.stdout = sys.stderr  # biblioteki mogą pisać postęp; stdout protokołu zawiera wyłącznie JSON
    root, pack, steps = sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 12
    try:
        sys.path.insert(0, root)
        import numpy as np
        import torch
        from omnivoice.models.omnivoice import OmniVoice

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = OmniVoice.from_pretrained(
            "k2-fsa/OmniVoice", device_map=device, dtype=torch.float16 if device == "mps" else torch.float32
        )
        with open(f"{pack}/voice.txt", encoding="utf-8") as handle:
            ref_text = handle.read().strip()
        prompt = model.create_voice_clone_prompt(f"{pack}/voice.wav", ref_text)
        model.generate(text="Lektor gotowy.", language="pl", voice_clone_prompt=prompt, num_step=steps)
    except Exception as exc:  # brak VoiceStudio / modelu / voicepacka — LiveDub zostaje przy Supertonic
        reply({"ready": False, "error": str(exc)[:200]})
        return
    reply({"ready": True, "rate": RATE, "device": device})
    voices = {}  # klucz postaci → voice_clone_prompt (próbka z dźwięku gry; tylko w pamięci tego procesu)
    embedder = Embedder()
    for raw in sys.stdin:
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            continue
        op = job.get("op")
        try:
            if op == "embed":
                reply({"id": job.get("id"), "ok": True, "emb": embedder.embed(job["wav"])})
                continue
            if op == "forget":
                voices.pop(job.get("key"), None)
                reply({"id": job.get("id"), "ok": True})
                continue
            if op == "voice":
                import time

                started = time.monotonic()
                voices[job["key"]] = model.create_voice_clone_prompt(job["wav"], job["text"])
                while len(voices) > 16:
                    del voices[next(iter(voices))]
                reply({"id": job.get("id"), "ok": True, "secs": round(time.monotonic() - started, 2)})
                continue
            speed = max(0.7, min(1.3, float(job.get("speed") or 1.0)))
            requested_voice = job.get("voice")
            if requested_voice and requested_voice not in voices:
                reply({"id": job.get("id"), "ok": False, "voice": None, "error": f"brak aktywnej próbki mówcy {requested_voice}"})
                continue
            chosen = voices[requested_voice] if requested_voice else prompt
            wav = model.generate(
                text=job["text"], language="pl", voice_clone_prompt=chosen, num_step=steps, speed=speed
            )[0]
            audio = np.clip(wav.squeeze().float().cpu().numpy(), -1.0, 1.0)
            with wave.open(job["out"], "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(RATE)
                handle.writeframes((audio * 32767.0).astype("<i2").tobytes())
            reply({"id": job.get("id"), "ok": True, "voice": requested_voice or "default"})
        except Exception as exc:
            reply({"id": job.get("id"), "ok": False, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
