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
