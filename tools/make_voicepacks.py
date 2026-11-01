#!/usr/bin/env python3
"""Nowe głosy lektora (voicepacki) z opisu — OmniVoice z VoiceStudio, bez klonowania prawdziwych osób.

Uruchom Pythonem z VoiceStudio:
  ~/voice-studio-projekty/VoiceStudio/.venv/bin/python tools/make_voicepacks.py [--force]

Każdy voicepack to voice.wav (~3 s wzorca), voice.txt (co w nim pada) i voice.json (opis). LiveDub pokazuje je
w „Więcej” → Głos → Voicepack.
"""
import json
import sys
import wave
from pathlib import Path

VOICESTUDIO = Path.home() / "voice-studio-projekty/VoiceStudio"
PACKS = Path.home() / "Library/Application Support/GameReader/voicepacks"
TEXT = "Nie wiem, co się tu wydarzyło, ale musimy stąd uciekać."
STEPS = 32

# nazwa → (opis dla OmniVoice, ziarno); lektor-M04 (male, middle-aged, very low pitch, seed 4) już jest
VOICES = {
    "lektor-spokojny": ("male, middle-aged, low pitch", 7),
    "lektor-mlody": ("male, young adult, low pitch", 3),
    "lektor-starszy": ("male, elderly, low pitch", 5),
    "lektorka": ("female, middle-aged, low pitch", 4),
    "lektorka-mloda": ("female, young adult, moderate pitch", 2),
}


def main():
    sys.path.insert(0, str(VOICESTUDIO))
    import numpy as np
    import torch
    from omnivoice.models.omnivoice import OmniVoice
    from omnivoice.utils.common import fix_random_seed

    force = "--force" in sys.argv
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=device, dtype=torch.float32)
    rate = getattr(model, "sampling_rate", 24000)
    for name, (instruct, seed) in VOICES.items():
        out = PACKS / name
        if (out / "voice.wav").exists() and not force:
            print(f"· {name}: już jest")
            continue
        fix_random_seed(seed)
        audio = model.generate(text=TEXT, language="pl", instruct=instruct, num_step=STEPS)[0]
        pcm = np.asarray(audio.detach().cpu() if hasattr(audio, "detach") else audio, dtype=np.float32).reshape(-1)
        pcm = pcm / max(1e-6, float(np.abs(pcm).max())) * 0.9
        out.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out / "voice.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes((pcm * 32767).astype(np.int16).tobytes())
        (out / "voice.txt").write_text(TEXT, encoding="utf-8")
        meta = {"deepen": 1.0, "opis": f"OmniVoice z opisu: {instruct}, seed {seed}"}
        (out / "voice.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        print(f"✓ {name}: {len(pcm) / rate:.1f} s — {instruct}")


if __name__ == "__main__":
    main()
