"""Test silnika na Windowsie (GitHub Actions): okna, zrzut, OCR Windowsa, lektor, tłumacz, worker.

Uruchamiany Pythonem zainstalowanym przez electron/setup.js — tak jak u użytkownika."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import gamereader_engine as eng  # noqa: E402
import gamereader_win as win  # noqa: E402

assert eng.IS_WIN, "to nie Windows"


def step(name):
    print(f"\n=== {name}", flush=True)


step("importy bibliotek natywnych (osobne procesy)")
# kolejność ładowania DLL (pywinrt ma własne msvcp140.dll) — pokazuje, co z czym się gryzie
for code in (
    "import ctranslate2",
    "import torch",
    "import argostranslate.translate",
    "import winrt.windows.media.ocr; import ctranslate2",
    "import winrt.windows.media.ocr; import torch",
    "import winrt.windows.media.ocr; import argostranslate.translate",
    "import argostranslate.translate; import winrt.windows.media.ocr",
):
    res = subprocess.run([sys.executable, "-X", "faulthandler", "-c", code], capture_output=True, text=True, timeout=300)
    tail = (res.stderr or "").strip().splitlines()[-6:]
    print(f"  kod {res.returncode:>11} | {code}" + ("".join("\n      " + line for line in tail) if res.returncode else ""))

step("okna")
windows = win.list_windows()
print(f"widocznych okien: {len(windows)}")
print("PS Remote Play:", win.find_remote_play())
print("przeglądarka:", win.find_browser(video_titles=eng.VIDEO_TITLES))
print("źródło:", eng.find_source_window("auto", "ps"))

step("zrzut ekranu")
frame = win.grab(0, 0, 320, 120)
print("zrzut:", None if frame is None else frame.shape)
assert frame is not None and frame.shape == (120, 320, 3), "mss nie zrzuca ekranu"

step("OCR Windows")
backend = win.WindowsOcrBackend()
assert not backend.error, backend.error
print("języki OCR:", backend.languages())
font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 38)
image = Image.new("RGB", (900, 110), (12, 12, 12))
ImageDraw.Draw(image).text((30, 30), "Where is our money? Let me know.", font=font, fill=(255, 255, 255))
bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
ocr = eng.WindowsOcr()
text = ocr.read(bgr)
print("OCR:", repr(text), "| bez polskiego OCR:", ocr.backend.missing_polish)
assert "money" in text.lower(), f"OCR nie przeczytał napisu: {text!r}"

# napis jak w filmie: biały z czarną obwódką na kolorowym, ruchliwym tle
rng = np.random.default_rng(3)
noisy = Image.fromarray(rng.integers(0, 255, (11, 90, 3)).astype(np.uint8)).resize((900, 110), Image.BILINEAR)
draw = ImageDraw.Draw(noisy)
for dx in (-2, 0, 2):
    for dy in (-2, 0, 2):
        draw.text((30 + dx, 30 + dy), "Where is our money? Let me know.", font=font, fill=(0, 0, 0))
draw.text((30, 30), "Where is our money? Let me know.", font=font, fill=(250, 250, 250))
video = ocr.read(np.ascontiguousarray(np.asarray(noisy)[:, :, ::-1]))
print("OCR na tle filmu:", repr(video))
assert "money" in video.lower(), f"OCR nie przeczytał napisu na tle filmu: {video!r}"

step("tłumacz EN→PL")
t0 = time.time()
pl = eng.ArgosTranslator().translate("Where is our money? Let me know.")
print(f"{pl!r} ({time.time() - t0:.1f}s)")
assert pl and pl.lower() != "where is our money? let me know.", "tłumacz nic nie zrobił"

step("lektor")
lektor = eng.MaleLektor()
t0 = time.time()
lektor.ensure()
path = lektor.prepare("Dzień dobry, to jest test lektora na Windowsie.")
print(f"{path} {Path(path).stat().st_size} B ({time.time() - t0:.1f}s) | {lektor.last_timing}")
assert Path(path).stat().st_size > 10000, "lektor nie nagrał głosu"

step("worker")
env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
proc = subprocess.Popen(
    [sys.executable, str(ROOT / "gamereader_worker.py")],
    cwd=str(ROOT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8",
)
ready = None
deadline = time.time() + 120
while time.time() < deadline:
    line = proc.stdout.readline()
    if not line:
        break
    msg = json.loads(line)
    print("  ", msg.get("event"), str(msg.get("text", ""))[:100])
    if msg.get("event") == "ready":
        ready = msg
        break
proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
proc.stdin.flush()
try:
    proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
if ready is None:
    print(proc.stderr.read()[-3000:])
assert ready is not None, "worker nie wysłał „ready”"

print("\nWSZYSTKO OK")
