#!/usr/bin/env python3
"""Zmierz naturalne tempo głosu (litery/s przy speed 1,0) i zapisz je w voice.json jako „lps”.

Głos z nagrania kopiuje tempo próbki, więc bez pomiaru jeden mówi 5,6, a inny 13 liter/s. Z „lps” LiveDub dobiera
speed tak, żeby każdy głos mówił jak lektor filmowy (OMNI_TARGET_LPS w gamereader_engine.py).

Uruchom Pythonem z VoiceStudio:
  ~/voice-studio-projekty/VoiceStudio/.venv/bin/python tools/voicepack_tempo.py lektor-filmowy-2 [lektor-filmowy-3 …]
"""
import json
import sys
from pathlib import Path

PACKS = Path.home() / "Library/Application Support/GameReader/voicepacks"
VOICESTUDIO = Path.home() / "voice-studio-projekty/VoiceStudio"
STEPS = 8  # jak OMNI_STEPS w LiveDub
LINES = [
    "Mamy coś dla niego odebrać.",
    "Nie wiem, co się tu wydarzyło, ale musimy stąd natychmiast uciekać.",
    "Dobra, słuchaj mnie uważnie. Jedziesz prosto do portu i czekasz na sygnał.",
    "To nie jest takie proste, jak ci się wydaje.",
    "Gliny są dwie przecznice stąd, a ja nie zamierzam wracać do pudła.",
    "Zostanie to między nami?",
]


def main():
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not names:
        sys.exit(__doc__)
    sys.path.insert(0, str(VOICESTUDIO))
    import numpy as np
    import torch
    from omnivoice.models.omnivoice import OmniVoice
    from omnivoice.utils.common import fix_random_seed

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=device, dtype=torch.float32)
    rate = getattr(model, "sampling_rate", 24000)
    for name in names:
        pack = PACKS / name
        prompt = model.create_voice_clone_prompt(str(pack / "voice.wav"), (pack / "voice.txt").read_text().strip())
        letters = seconds = 0.0
        for i, text in enumerate(LINES):
            fix_random_seed(i)
            audio = model.generate(text=text, language="pl", voice_clone_prompt=prompt, num_step=STEPS, speed=1.0)[0]
            pcm = np.asarray(audio.detach().cpu() if hasattr(audio, "detach") else audio, dtype=np.float32).reshape(-1)
            loud = np.flatnonzero(np.abs(pcm) > 0.01)  # sama mowa, bez ciszy na brzegach
            if loud.size:
                seconds += (loud[-1] - loud[0]) / rate
                letters += sum(ch.isalpha() for ch in text)
        lps = round(letters / max(seconds, 0.1), 2)
        meta_path = pack / "voice.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta["lps"] = lps
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        print(f"✓ {name}: {lps} liter/s przy speed 1,0")


if __name__ == "__main__":
    main()
