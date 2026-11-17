#!/usr/bin/env python3
"""Głos lektora z własnego nagrania (np. fragment polskiego filmu z lektorem) — tylko do prywatnego użytku.

Uruchom Pythonem silnika (ma Parakeet do spisania tekstu wzorca):
  ~/gamer/gr/bin/python tools/voicepack_from_clip.py FILM.mkv lektor-filmowy [--start 12:30] [--end 12:45] [--text "…"]

--style zywy dokłada do istniejącego głosu próbkę w innym rejestrze (voice-zywy.wav/.txt): LiveDub czyta nią
kwestie z „!”, „?” i mocniej przeżywane, resztę zwykłą próbką (voice.wav).

Bez --start/--end bierze cały plik i sam wybiera najczystsze ~6 s mowy. Najlepiej wskazać fragment, gdzie lektor
mówi sam, bez muzyki i głosów aktorów pod spodem. Wynik trafia do voicepacks/<nazwa> (voice.wav, voice.txt,
voice.json) na tym Macu — nie do repo ani do aplikacji. LiveDub pokazuje go w „Więcej” → Głos → Voicepack.
"""
import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
PACKS = Path.home() / "Library/Application Support/GameReader/voicepacks"
RATE = 24000  # OmniVoice
WINDOW = 6.0  # s wzorca: krótki = szybka synteza (OMNI_STEPS 8 ≈ 0,6–0,8 s na kwestię)
# 9 s (--length 9): głos bliżej oryginału (podobieństwo do lektora 0,646 → 0,715, test 18.11), +0,4–1 s na kwestię


def seconds(value):
    if value is None:
        return None
    parts = [float(p) for p in str(value).split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def load(path, start, end, rate):
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def cleanest_window(audio, rate, length):
    """Okno z największym udziałem mowy i najmniej skokami głośności (muzyka, cisza)."""
    if audio.size <= int(rate * length):
        return audio
    frame = int(rate * 0.032)
    n = audio.size // frame
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    gate = max(1e-3, float(np.percentile(rms, 70)) * 0.25)
    voiced = (rms > gate).astype(np.float32)
    span = int(length / 0.032)
    best, best_score = 0, -1.0
    for i in range(0, n - span, 4):
        win = rms[i : i + span]
        score = float(voiced[i : i + span].mean()) - 0.5 * float(np.std(win) / (np.mean(win) + 1e-6))
        if score > best_score:
            best, best_score = i, score
    return audio[best * frame : (best + span) * frame]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("name")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--text", help="co pada we wzorcu (domyślnie spisuje Parakeet)")
    ap.add_argument("--deepen", type=float, default=1.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--style", help="rejestr dokładany do istniejącego głosu, np. zywy")
    ap.add_argument("--length", type=float, default=WINDOW, help="ile sekund mowy wziąć do wzorca (domyślnie 6)")
    args = ap.parse_args()

    name = "".join(ch for ch in args.name if ch.isalnum() or ch in "-_")
    out = PACKS / name
    style = "".join(ch for ch in (args.style or "") if ch.isalnum())
    stem = f"voice-{style}" if style else "voice"
    if style and not (out / "voice.wav").exists():
        sys.exit(f"{out} nie istnieje — najpierw zrób zwykłą próbkę (bez --style)")
    if (out / f"{stem}.wav").exists() and not args.force:
        sys.exit(f"{out / stem}.wav już jest — dodaj --force, żeby nadpisać")
    start, end = seconds(args.start), seconds(args.end)
    audio = cleanest_window(load(args.clip, start, end, RATE), RATE, args.length)
    if audio.size < RATE * 2:
        sys.exit("za krótki fragment — potrzeba co najmniej 2 s mowy lektora")
    audio = audio / max(1e-6, float(np.abs(audio).max())) * 0.9

    text = args.text
    if not text:
        import gamereader_engine as ge

        mono16 = np.interp(np.arange(0, audio.size, RATE / ge.SAMPLE_RATE), np.arange(audio.size), audio)
        text = ge.ParakeetSTT().transcribe(mono16.astype(np.float32))
    if not text:
        sys.exit("nie udało się spisać tekstu wzorca — podaj go przez --text")

    out.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out / f"{stem}.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((audio * 32767).astype(np.int16).tobytes())
    (out / f"{stem}.txt").write_text(text.strip(), encoding="utf-8")
    if not style:
        meta = {"deepen": args.deepen, "opis": f"z nagrania {Path(args.clip).name} — prywatnie, nie udostępniać"}
        (out / "voice.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"✓ {name}{' / ' + style if style else ''}: {audio.size / RATE:.1f} s — „{text.strip()}”")
    print(f"  odsłuch: afplay '{out / (stem + '.wav')}'; tempo: tools/voicepack_tempo.py {name}")


if __name__ == "__main__":
    main()
