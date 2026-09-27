#!/usr/bin/env python3
"""Lektor z VoiceStudio (OmniVoice) jako proces w tle LiveDub.

Uruchamiany Pythonem z VoiceStudio: model ładuje się raz, głos to „voicepack” — wzorcowe nagranie
(voice.wav + voice.txt) wygenerowane z opisu, więc każda kwestia brzmi tym samym lektorem.

Protokół (JSON w liniach): stdin {"id", "text", "speed", "out"} → stdout {"id", "ok", "error"?}.
Pierwsza linia na stdout: {"ready": true, "rate": 24000} albo {"ready": false, "error": "..."}.
"""
import json
import sys
import wave

RATE = 24000


def reply(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
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
    for raw in sys.stdin:
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            speed = max(0.7, min(1.3, float(job.get("speed") or 1.0)))
            wav = model.generate(
                text=job["text"], language="pl", voice_clone_prompt=prompt, num_step=steps, speed=speed
            )[0]
            audio = np.clip(wav.squeeze().float().cpu().numpy(), -1.0, 1.0)
            with wave.open(job["out"], "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(RATE)
                handle.writeframes((audio * 32767.0).astype("<i2").tobytes())
            reply({"id": job.get("id"), "ok": True})
        except Exception as exc:
            reply({"id": job.get("id"), "ok": False, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
