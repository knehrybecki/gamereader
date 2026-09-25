import hashlib
import wave
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path

import numpy as np
from PIL import Image, ImageTk


POLL_MS = 80
MIN_INTERVAL = 0.25
DEFAULT_INTERVAL = 0.4
BLACK_MEAN = 8.0
PREVIEW_W = 780
PREVIEW_H = 110
CONFIG_PATH = Path.home() / "Library/Application Support/GameReader/config.json"
CACHE_DIR = Path.home() / "Library/Caches/GameReader"
VOICE_DIR = Path.home() / "Library/Application Support/GameReader/voices"
JARVIS_ONNX = VOICE_DIR / "pl_PL-jarvis_wg_glos-medium.onnx"

MOODS = {
    "narrate": {"label": "spokojny", "length": 1.10, "volume": 0.96, "noise": 0.55},
    "urgent": {"label": "ostry", "length": 0.84, "volume": 1.18, "noise": 0.85},
    "intense": {"label": "gniew", "length": 0.78, "volume": 1.25, "noise": 1.05},
    "question": {"label": "pytanie", "length": 1.04, "volume": 1.02, "noise": 0.65},
    "soft": {"label": "cicho", "length": 1.26, "volume": 0.70, "noise": 0.35},
    "tender": {"label": "ciepło", "length": 1.16, "volume": 0.88, "noise": 0.40},
    "dark": {"label": "mrocznie", "length": 1.22, "volume": 0.82, "noise": 0.75},
}

MOOD_WORDS_EN = {
    "intense": (
        "hate", "kill", "die", "murder", "bastard", "damn", "fuck", "shit", "hell",
        "attack", "destroy", "burn", "bleed", "idiot", "stupid",
    ),
    "dark": (
        "blood", "corpse", "fear", "afraid", "darkness", "death", "ghost",
        "monster", "nightmare", "scream", "pain", "suffer", "grave", "shadow",
    ),
    "tender": (
        "love", "please", "sorry", "thanks", "forgive", "miss", "friend",
        "together", "honey", "dear",
    ),
    "urgent": (
        "run", "hurry", "quick", "move", "fast", "behind", "incoming", "help",
    ),
    "soft": ("quiet", "whisper", "sleep", "slowly", "calm"),
}

MOOD_WORDS_PL = {
    "intense": (
        "nienawidzę", "zabij", "zabić", "zabiję", "śmierć", "zdychaj", "won",
        "giń", "atak", "zniszcz", "kretyn", "idiota", "zamknij",
    ),
    "dark": (
        "krew", "trup", "strach", "boję", "ciemność", "zginiesz", "umrzesz",
        "duch", "potwór", "koszmar", "krzyk", "grób", "cień",
    ),
    "tender": (
        "kocham", "kochanie", "przepraszam", "tęsknię", "proszę", "dziękuję",
        "dzięki", "wybacz", "przyjaciel",
    ),
    "urgent": (
        "uciekaj", "szybko", "ruszaj", "uwaga", "pomocy", "stój", "natychmiast",
    ),
    "soft": ("cicho", "szept", "śpij", "odpocznij", "powoli", "spokojnie"),
}

BG = "#0B0D12"
SURFACE = "#141821"
SURFACE2 = "#1B2130"
BORDER = "#2A3344"
TEXT = "#F4F1EA"
MUTED = "#8B93A7"
ACCENT = "#7CFFD0"
GOLD = "#F5C16C"
FONT = "Helvetica Neue"
FONT_ALT = "SF Pro Display"


def ui_font(size, weight="normal"):
    try:
        return (FONT_ALT, size, weight)
    except tk.TclError:
        return (FONT, size, weight)


def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def text_key(text):
    return hashlib.sha1(normalize_text(text).lower().encode("utf-8")).hexdigest()


def is_black_frame(frame):
    return frame is None or frame.size == 0 or float(np.mean(frame)) < BLACK_MEAN


PL_MARK = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
EN_HINTS = {
    "the", "you", "and", "that", "this", "what", "have", "don't", "it's", "we",
    "i", "to", "of", "is", "are", "was", "were", "not", "your", "my", "me",
}


def should_translate(text):
    raw = normalize_text(text)
    if not raw or any(ch in PL_MARK for ch in raw):
        return False
    tokens = set(re.findall(r"[a-z']+", raw.lower()))
    return len(tokens & EN_HINTS) >= 1 or len(tokens) >= 4


def usable_ocr(text):
    raw = normalize_text(text)
    if len(raw) < 3:
        return False
    if re.fullmatch(r"[\d\s:%./+\-–—]+", raw):
        return False
    return sum(ch.isalpha() for ch in raw) >= 3


def _score_words(scores, text, lexicon):
    if not text:
        return
    low = text.lower()
    tokens = set(re.findall(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ']+", low))
    for mood, words in lexicon.items():
        for word in words:
            if word in tokens:
                scores[mood] += 2 if len(word) > 3 else 1
            elif len(word) > 5 and any(token.startswith(word) for token in tokens):
                scores[mood] += 1


def detect_mood(polish, english=None):
    raw = normalize_text(" ".join(part for part in (polish, english) if part))
    if not raw:
        return "narrate"
    scores = {name: 0 for name in MOODS}

    if "?!" in raw or "!?" in raw:
        scores["intense"] += 4
    bangs = raw.count("!")
    if bangs >= 2:
        scores["urgent"] += 3
        scores["intense"] += 2
    elif bangs == 1:
        scores["urgent"] += 2
    if "?" in raw:
        scores["question"] += 3
    if raw.endswith("...") or raw.endswith("…") or "…" in raw:
        scores["soft"] += 3

    _score_words(scores, polish, MOOD_WORDS_PL)
    _score_words(scores, english, MOOD_WORDS_EN)

    if scores["question"] and scores["urgent"]:
        scores["urgent"] += 1
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "narrate"
    return best


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(data):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


SAMPLE_RATE = 16000
SILENCE_SEC = 0.12
MIN_SPEECH_SEC = 0.18
MAX_SPEECH_SEC = 2.2
STREAM_SEC = 0.24
PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
SPEECH_RMS = 0.0025
LIVE_WORDS = 3
HOLD_WORDS = 1
JUNK_HEARD = {
    "thanks for watching",
    "thank you",
    "you",
    "thank you.",
    "thanks for watching.",
    "bye",
    "thanks",
}


def list_input_devices():
    import sounddevice as sd

    devices = []
    for index, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) > 0:
            devices.append((index, info["name"]))
    return devices


PS_REMOTE = "PS Remote Play"


def which_bin(name):
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        path = Path(folder) / name
        if path.is_file():
            return str(path)
    return None


def _speakable(piece):
    piece = normalize_text(piece)
    if not piece or piece.lower() in JUNK_HEARD:
        return False
    return bool(re.search(r"[A-Za-zĄąĆćĘęŁłŃńÓóŚśŹźŻż]", piece))


def take_ready(full, emitted, force=False):
    full = normalize_text(full)
    emitted = normalize_text(emitted)
    if not full:
        return [], emitted
    low_full = full.lower()
    if emitted and low_full.startswith(emitted.lower()):
        rest = normalize_text(full[len(emitted) :])
    else:
        rest = full
        emitted = ""
    ready = []
    while True:
        match = re.search(r"[,:;—.!?]", rest)
        if not match:
            break
        piece = normalize_text(rest[: match.end()])
        rest = normalize_text(rest[match.end() :])
        if _speakable(piece):
            ready.append(piece)
            emitted = normalize_text(f"{emitted} {piece}")
    words = rest.split()
    if force:
        piece = normalize_text(rest)
        if _speakable(piece):
            ready.append(piece)
            emitted = normalize_text(f"{emitted} {piece}")
    elif len(words) >= LIVE_WORDS + HOLD_WORDS:
        piece = " ".join(words[:-HOLD_WORDS])
        if _speakable(piece):
            ready.append(piece)
            emitted = normalize_text(f"{emitted} {piece}")
    return ready, emitted


def preferred_device_name(devices):
    return PS_REMOTE


def remote_play_bounds():
    try:
        import Quartz

        options = Quartz.kCGWindowListOptionOnScreenOnly
        windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
        best = None
        best_area = 0
        for info in windows:
            owner = str(info.get("kCGWindowOwnerName") or "").lower()
            if not any(key in owner for key in ("remoteplay", "remote play", "playstation")):
                continue
            bounds = info.get("kCGWindowBounds") or {}
            width = float(bounds.get("Width") or 0)
            height = float(bounds.get("Height") or 0)
            area = width * height
            if height > 80 and area > best_area:
                best_area = area
                best = (
                    int(bounds.get("X") or 0),
                    int(bounds.get("Y") or 0),
                    int(width),
                    int(height),
                )
        if best:
            return best
    except Exception:
        pass
    script = (
        'tell application "System Events"\n'
        'repeat with proc in (application processes whose bundle identifier is "com.playstation.RemotePlay" '
        'or name contains "RemotePlay")\n'
        "if (count of windows of proc) > 0 then\n"
        "set w to window 1 of proc\n"
        "set p to position of w\n"
        "set s to size of w\n"
        'return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & '
        '((item 1 of s) as text) & "," & ((item 2 of s) as text)\n'
        "end if\n"
        "end repeat\n"
        "end tell"
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    parts = [piece.strip() for piece in (result.stdout or "").split(",") if piece.strip()]
    if len(parts) == 4:
        return tuple(int(float(piece)) for piece in parts)
    return None


def main_display_bounds():
    try:
        import Quartz

        display = Quartz.CGMainDisplayID()
        rect = Quartz.CGDisplayBounds(display)
        return (int(rect.origin.x), int(rect.origin.y), int(rect.size.width), int(rect.size.height))
    except Exception:
        return (0, 0, 1512, 982)


def subtitle_band(bounds, frac=0.18, gap_frac=0.035):
    left, top, width, height = bounds
    band_h = max(64, int(height * frac))
    inset = max(20, int(width * 0.08))
    gap = max(6, int(height * gap_frac))
    return (left + inset, top + height - band_h - gap, max(80, width - 2 * inset), band_h)


def subtitle_band_candidates(bounds):
    return [
        subtitle_band(bounds, 0.12, 0.03),
        subtitle_band(bounds, 0.16, 0.04),
        subtitle_band(bounds, 0.22, 0.02),
    ]


def game_reader_bin():
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "MacOS" / "GameReader",
        Path.home() / "Applications/GameReader.app/Contents/MacOS/GameReader",
        Path("/Users/kamil/gamer/macos/GameReader"),
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def audio_tap_cmd():
    path = game_reader_bin()
    return [str(path), "--tap"] if path else None


def helper_sock_path():
    env = os.environ.get("GAMEREADER_SOCK")
    if env:
        return Path(env)
    return Path.home() / "Library/Application Support/GameReader/helper.sock"


def helper_available():
    path = helper_sock_path()
    return path.exists() or path.is_socket()


def helper_call(cmd, timeout=20):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(helper_sock_path()))
    sock.sendall((cmd.rstrip() + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    return line.decode("utf-8", "replace").strip(), sock, rest


def helper_has_screen():
    if not helper_available():
        return None
    try:
        line, sock, _rest = helper_call("PERM", timeout=3)
        sock.close()
        if line == "OK":
            return True
        if line == "NEED":
            return False
    except OSError:
        return None
    return None


def pick_region_native():
    if helper_available():
        try:
            line, sock, _rest = helper_call("PICK", timeout=180)
            sock.close()
            if line.startswith("OK "):
                parts = [piece.strip() for piece in line[3:].split(",") if piece.strip()]
                if len(parts) == 4:
                    return tuple(int(float(piece)) for piece in parts)
            return None
        except OSError:
            pass
    path = game_reader_bin()
    if path is None:
        return None
    result = subprocess.run([str(path), "--pick"], capture_output=True, text=True)
    parts = [piece.strip() for piece in (result.stdout or "").split(",") if piece.strip()]
    if len(parts) == 4:
        return tuple(int(float(piece)) for piece in parts)
    return None


class GameAudioTap:
    def __init__(self):
        self.proc = None
        self.sock = None
        self.chunks = queue.Queue()
        self.message = ""
        self.alive = False

    def start(self):
        if helper_available():
            self._start_helper()
            return
        cmd = audio_tap_cmd()
        if cmd is None:
            raise RuntimeError("Brak GameReader --tap. Przebuduj aplikację.")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.alive = True
        threading.Thread(target=self._read_audio, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        deadline = time.time() + 8
        while time.time() < deadline and self.proc.poll() is None:
            time.sleep(0.1)
            if self.message:
                break
        if self.proc.poll() is not None:
            text = self.message or "PS Remote Play nie oddaje dźwięku."
            if any(word in text.lower() for word in ("tcc", "zgody", "przechwytywania", "denied", "not permitted")):
                raise PermissionError(text)
            raise RuntimeError(text)

    def _start_helper(self):
        line, sock, rest = helper_call("TAP", timeout=12)
        if line.startswith("ERR"):
            sock.close()
            text = line[4:].strip() or "PS Remote Play nie oddaje dźwięku."
            if any(word in text.lower() for word in ("tcc", "zgody", "przechwytywania", "denied", "not permitted", "need")):
                raise PermissionError(text)
            raise RuntimeError(text)
        if not line.startswith("OK"):
            sock.close()
            raise RuntimeError(line or "Tap nie odpowiedział.")
        self.message = line[3:].strip()
        self.sock = sock
        self.alive = True
        threading.Thread(target=self._read_audio_sock, args=(rest,), daemon=True).start()

    def _read_audio_sock(self, leftover):
        buf = leftover
        sock = self.sock
        try:
            sock.settimeout(0.4)
            while self.alive and sock is not None:
                try:
                    piece = sock.recv(4096)
                except socket.timeout:
                    continue
                if not piece:
                    break
                buf += piece
                usable = len(buf) - (len(buf) % 4)
                if usable <= 0:
                    continue
                audio = np.frombuffer(buf[:usable], dtype="<f4").copy()
                buf = buf[usable:]
                if audio.size:
                    self.chunks.put(audio)
        except OSError:
            pass
        finally:
            self.alive = False

    def _read_audio(self):
        handle = self.proc.stdout
        leftover = b""
        while self.proc and self.proc.poll() is None:
            piece = handle.read(4096)
            if not piece:
                break
            leftover += piece
            usable = len(leftover) - (len(leftover) % 4)
            if usable <= 0:
                continue
            audio = np.frombuffer(leftover[:usable], dtype="<f4").copy()
            leftover = leftover[usable:]
            if audio.size:
                self.chunks.put(audio)

    def _read_err(self):
        for line in iter(self.proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.message = text

    def read(self, timeout=0.05):
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        self.alive = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def open_mic_settings():
    urls = [
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
        "x-apple.systempreferences:com.apple.Settings.PrivacySecurity.Privacy.Microphone",
    ]
    for url in urls:
        if subprocess.run(["open", url], capture_output=True).returncode == 0:
            return
    subprocess.run(["open", "/System/Applications/System Settings.app"], check=False)


def open_screen_settings():
    urls = [
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        "x-apple.systempreferences:com.apple.Settings.PrivacySecurity.Privacy.ScreenCapture",
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
    ]
    for url in urls:
        if subprocess.run(["open", url], capture_output=True).returncode == 0:
            return
    subprocess.run(["open", "/System/Applications/System Settings.app"], check=False)


def set_mac_app_name():
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle

        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "GameReader"
            info["CFBundleDisplayName"] = "GameReader"
            info["LSUIElement"] = True
    except Exception:
        pass


def hide_helper_dock_icon():
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


class MaleLektor:
    def __init__(self):
        self.player = None
        self.lock = threading.Lock()
        self.voice = None
        self.ffmpeg = which_bin("ffmpeg")

    def ensure(self):
        if self.voice is not None:
            return
        from piper import PiperVoice

        if not JARVIS_ONNX.is_file():
            raise RuntimeError("Brak lokalnego głosu lektora (Piper Jarvis).")
        self.voice = PiperVoice.load(str(JARVIS_ONNX))

    def stop(self):
        with self.lock:
            if self.player and self.player.poll() is None:
                self.player.terminate()
            self.player = None
        subprocess.run(["killall", "afplay"], capture_output=True)

    def wait(self):
        with self.lock:
            proc = self.player
        if proc is not None:
            proc.wait()

    def prepare(self, text, source=None, volume=1.0):
        self.ensure()
        mood = detect_mood(text, source)
        profile = MOODS[mood]
        length = float(profile["length"])
        vol = max(0.45, min(1.35, float(profile["volume"]) * float(volume)))
        noise = float(profile["noise"])
        path = CACHE_DIR / f"{text_key(text + mood + f'{length:.2f}{vol:.2f}')}.wav"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size < 64:
            self._synth(text, path, length, vol, noise)
        return mood, path

    def speak(self, text, source=None, volume=1.0):
        mood, path = self.prepare(text, source=source, volume=volume)
        self.stop()
        self._play_file(path)
        return mood

    def _synth(self, text, path, length, volume, noise):
        from piper import SynthesisConfig

        cfg = SynthesisConfig(length_scale=length, volume=volume, noise_w_scale=noise)
        with wave.open(str(path), "wb") as handle:
            self.voice.synthesize_wav(text, handle, syn_config=cfg)
        if not path.exists() or path.stat().st_size < 64:
            raise RuntimeError("Nie udało się zsyntetyzować lektora.")

    def _play_file(self, path):
        with self.lock:
            self.player = subprocess.Popen(
                ["afplay", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


class ArgosTranslator:
    def __init__(self):
        self._fn = None

    def ensure(self):
        if self._fn is not None:
            return
        import argostranslate.package as argos_package
        import argostranslate.translate as argos_translate

        installed = argos_translate.get_installed_languages()
        src = next((lang for lang in installed if lang.code == "en"), None)
        dst = next((lang for lang in installed if lang.code == "pl"), None)
        if src is None or dst is None:
            argos_package.update_package_index()
            available = argos_package.get_available_packages()
            pkg = next((item for item in available if item.from_code == "en" and item.to_code == "pl"), None)
            if pkg is None:
                raise RuntimeError("Nie znaleziono pakietu tłumaczeń en→pl.")
            argos_package.install_from_path(pkg.download())
            installed = argos_translate.get_installed_languages()
            src = next((lang for lang in installed if lang.code == "en"), None)
            dst = next((lang for lang in installed if lang.code == "pl"), None)
        pair = src.get_translation(dst) if src and dst else None
        if pair is None:
            raise RuntimeError("Nie udało się załadować tłumacza en→pl.")
        self._fn = pair.translate

    def translate(self, text):
        self.ensure()
        return normalize_text(self._fn(text))


class ParakeetSTT:
    def __init__(self):
        self.model = None
        self._cm = None
        self._stream = None

    def ensure(self):
        if self.model is not None:
            return
        from parakeet_mlx import from_pretrained

        self.model = from_pretrained(PARAKEET_MODEL)

    def warmup(self):
        self.ensure()
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        dummy = mx.array(np.zeros(int(SAMPLE_RATE * 0.7), dtype=np.float32))
        self.model.generate(get_logmel(dummy, self.model.preprocessor_config))

    def start_stream(self):
        self.close_stream()
        self.ensure()
        self._cm = self.model.transcribe_stream(context_size=(128, 128))
        self._stream = self._cm.__enter__()

    def close_stream(self):
        if self._cm is None:
            return
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass
        self._cm = None
        self._stream = None

    def _norm_audio(self, audio):
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            audio = audio / peak
        return audio

    def push(self, audio):
        import mlx.core as mx

        if self._stream is None:
            self.start_stream()
        audio = self._norm_audio(audio)
        if audio.size < int(SAMPLE_RATE * 0.12):
            return normalize_text(self._stream.result.text)
        self._stream.add_audio(mx.array(audio))
        return normalize_text(self._stream.result.text)

    def finish(self, tail):
        text = ""
        if tail is not None and tail.size >= int(SAMPLE_RATE * 0.12):
            text = self.push(tail)
        elif self._stream is not None:
            text = normalize_text(self._stream.result.text)
        self.close_stream()
        return text

    def transcribe(self, audio):
        self.ensure()
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        audio = self._norm_audio(audio)
        if audio.size < int(SAMPLE_RATE * 0.2):
            return ""
        results = self.model.generate(get_logmel(mx.array(audio), self.model.preprocessor_config))
        if not results:
            return ""
        return normalize_text(getattr(results[0], "text", None) or "")


class LiveTranscriber:
    def __init__(self, stt, on_line, on_draft=None, on_error=None, on_ready=None):
        self.stt = stt
        self.on_line = on_line
        self.on_draft = on_draft
        self.on_error = on_error
        self.on_ready = on_ready
        self.jobs = queue.Queue()
        self.emitted = ""
        self.thread = None
        self.ready = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def feed(self, mono):
        self.jobs.put(("pcm", mono))

    def flush(self):
        self.jobs.put(("flush", None))

    def stop(self):
        self.jobs.put(("stop", None))

    def _run(self):
        acc = []
        samples = 0
        window = int(SAMPLE_RATE * STREAM_SEC)
        try:
            import mlx.core as mx

            try:
                mx.set_default_device(mx.gpu)
            except Exception:
                pass
            self.stt.ensure()
            self.stt.warmup()
            self.ready.set()
            if self.on_ready:
                self.on_ready()
            while True:
                kind, data = self.jobs.get()
                if kind == "stop":
                    self.stt.close_stream()
                    return
                if kind == "pcm":
                    acc.append(data)
                    samples += data.size
                    if samples >= window:
                        text = self.stt.push(np.concatenate(acc))
                        acc = []
                        samples = 0
                        if self.on_draft and text:
                            self.on_draft(text)
                        self._emit(text, force=False)
                elif kind == "flush":
                    tail = np.concatenate(acc) if acc else None
                    acc = []
                    samples = 0
                    text = self.stt.finish(tail)
                    if self.on_draft and text:
                        self.on_draft(text)
                    self._emit(text, force=True)
                    self.emitted = ""
        except Exception as exc:
            self.ready.set()
            if self.on_error:
                self.on_error(str(exc))
            try:
                self.stt.close_stream()
            except Exception:
                pass

    def _emit(self, text, force):
        pieces, self.emitted = take_ready(text, self.emitted, force=force)
        for piece in pieces:
            self.on_line(piece)


class AppleVisionOcr:
    def _variants(self, image):
        from PIL import ImageEnhance, ImageFilter, ImageOps

        w, h = image.size
        if h < 160:
            scale = 160 / max(h, 1)
            image = image.resize((max(8, int(w * scale)), max(8, int(h * scale))), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(image)
        sharp = gray.filter(ImageFilter.UnsharpMask(radius=1.6, percent=180, threshold=2))
        return [
            image.convert("RGB"),
            ImageOps.autocontrast(image).convert("RGB"),
            ImageOps.autocontrast(ImageEnhance.Contrast(gray).enhance(2.3)).convert("RGB"),
            ImageOps.autocontrast(ImageOps.invert(sharp)).convert("RGB"),
            ImageOps.autocontrast(ImageEnhance.Contrast(ImageOps.invert(gray)).enhance(2.1)).convert("RGB"),
        ]

    def _run(self, image, framework, recognition_level="accurate", languages=None):
        from ocrmac import ocrmac

        kwargs = {"framework": framework, "detail": True}
        if framework == "vision":
            kwargs["recognition_level"] = recognition_level
            kwargs["confidence_threshold"] = 0.12
            if languages:
                kwargs["language_preference"] = languages
        elif languages:
            kwargs["language_preference"] = languages
        rows = ocrmac.OCR(image, **kwargs).recognize()
        return normalize_text(" ".join(str(row[0]) for row in rows if row))

    def read(self, frame):
        if frame is None or frame.size == 0:
            return ""
        rgb = frame[:, :, ::-1] if frame.shape[-1] == 3 else frame
        image = Image.fromarray(np.ascontiguousarray(rgb))
        variants = self._variants(image)
        for candidate in variants[:3]:
            try:
                text = self._run(candidate, "vision", "fast")
            except Exception:
                continue
            if usable_ocr(text):
                return text
        for candidate in variants:
            for langs in (None, ["en-US"], ["pl-PL"]):
                try:
                    text = self._run(candidate, "vision", "accurate", langs)
                except Exception:
                    continue
                if usable_ocr(text):
                    return text
        try:
            text = self._run(variants[3] if len(variants) > 3 else variants[0], "livetext")
            if usable_ocr(text):
                return text
        except Exception:
            pass
        return ""


class PillButton(tk.Canvas):
    def __init__(self, master, text, command, primary=False, width=168, height=40):
        super().__init__(master, width=width, height=height, highlightthickness=0, bg=master["bg"], cursor="hand2")
        self.command = command
        self.primary = primary
        self.width = width
        self.height = height
        self.enabled = True
        self.text = text
        self._draw(False)
        self.bind("<Enter>", lambda _e: self._draw(True))
        self.bind("<Leave>", lambda _e: self._draw(False))
        self.bind("<Button-1>", self._click)

    def _colors(self, hover):
        if self.primary:
            return ("#9BFFE0" if hover else ACCENT), "#08211B"
        return (SURFACE2 if not hover else "#243049"), TEXT

    def _draw(self, hover):
        fill, fg = self._colors(hover)
        self.delete("all")
        self.create_round_rect(1, 1, self.width - 2, self.height - 2, 12, fill=fill, outline=BORDER)
        self.create_text(self.width / 2, self.height / 2, text=self.text, fill=fg, font=ui_font(13, "bold"))

    def create_round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _click(self, _event):
        if self.command:
            self.command()

    def set_text(self, text):
        self.text = text
        self._draw(False)


class Toggle(tk.Frame):
    def __init__(self, master, text, variable):
        super().__init__(master, bg=master["bg"])
        self.variable = variable
        self.canvas = tk.Canvas(self, width=44, height=26, highlightthickness=0, bg=master["bg"], cursor="hand2")
        self.canvas.pack(side="left")
        tk.Label(self, text=text, fg=TEXT, bg=master["bg"], font=ui_font(12)).pack(side="left", padx=(8, 0))
        self.canvas.bind("<Button-1>", lambda _e: self.variable.set(not self.variable.get()))
        self.variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self):
        on = bool(self.variable.get())
        self.canvas.delete("all")
        self.canvas.create_oval(2, 2, 42, 24, fill=ACCENT if on else "#2A3140", outline="")
        x = 30 if on else 14
        self.canvas.create_oval(x - 9, 4, x + 9, 22, fill=BG, outline="")


class RegionSelector:
    def __init__(self, master):
        self.result = None
        self.start = None
        self.rect = None
        self._local = (0, 0)
        self.win = tk.Toplevel(master)
        self.win.withdraw()
        sw = master.winfo_screenwidth()
        sh = master.winfo_screenheight()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{sw}x{sh}+0+0")
        bg = "black"
        try:
            self.win.attributes("-transparent", True)
            bg = "systemTransparent"
            self.win.configure(bg=bg)
        except tk.TclError:
            self.win.attributes("-alpha", 0.14)
            self.win.configure(bg="#000000")
        self.canvas = tk.Canvas(self.win, bg=bg, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            sw // 2,
            56,
            text="Widać grę — przeciągnij pasek napisów   ·   Esc anuluje",
            fill=ACCENT,
            font=ui_font(20, "bold"),
        )
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", self._cancel)
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        try:
            self.win.grab_set()
        except tk.TclError:
            pass
        self.win.update()

    def _press(self, event):
        self.start = (event.x_root, event.y_root)
        if self.rect:
            self.canvas.delete(self.rect)
        self._local = (self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        self.rect = self.canvas.create_rectangle(*self._local, *self._local, outline=ACCENT, width=3)

    def _drag(self, event):
        if self.rect is None:
            return
        self.canvas.coords(
            self.rect,
            self._local[0],
            self._local[1],
            self.canvas.canvasx(event.x),
            self.canvas.canvasy(event.y),
        )

    def _release(self, event):
        if self.start is None:
            return
        x1, y1 = self.start
        x2, y2 = event.x_root, event.y_root
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        self._close()
        self.result = (int(left), int(top), int(width), int(height)) if width >= 20 and height >= 12 else None

    def _cancel(self, _event=None):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        self.win.destroy()

    def pick(self):
        self.win.wait_window()
        return self.result


class OverlayWindow:
    def __init__(self, master):
        self.win = tk.Toplevel(master)
        self.win.title("Lektor")
        self.win.attributes("-topmost", True)
        self.win.geometry("860x150+36+28")
        self.win.configure(bg="#111318")
        tk.Label(self.win, text=" ⋮⋮  trzymaj to okno z dala od napisów", fg=MUTED, bg="#111318", font=ui_font(11)).pack(
            fill="x", pady=(8, 0)
        )
        self.label = tk.Label(
            self.win,
            text="Czekam na polski napis…",
            fg=TEXT,
            bg="#111318",
            font=ui_font(22, "bold"),
            wraplength=820,
            justify="left",
            anchor="nw",
        )
        self.label.pack(fill="both", expand=True, padx=18, pady=12)

    def set_text(self, text):
        self.label.configure(text=text or "—")

    def withdraw(self):
        if self.win.winfo_exists():
            self.win.withdraw()

    def deiconify(self):
        if self.win.winfo_exists():
            self.win.deiconify()

    def destroy(self):
        if self.win.winfo_exists():
            self.win.destroy()


class GameReaderApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.cfg = load_config()
        self.region = tuple(self.cfg["region"]) if self.cfg.get("region") else None
        self.running = False
        self.worker = None
        self.overlay = None
        self.last_key = ""
        self.subtitle_until = 0.0
        self.ocr = AppleVisionOcr()
        self.lektor = MaleLektor()
        self.translator = ArgosTranslator()
        self.stt = ParakeetSTT()
        self.line_q = queue.Queue()
        self.transcriber = None
        self.preview_photo = None
        self.devices = list_input_devices()
        threading.Thread(target=self._tts_loop, daemon=True).start()
        self._build()
        self._restore()
        self.root.after(POLL_MS, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        if sys.platform == "darwin":
            try:
                self.root.createcommand("tk::mac::Quit", self._quit)
            except tk.TclError:
                pass

    def _persist(self):
        save_config(
            {
                "region": list(self.region) if self.region else None,
                "overlay": bool(self.overlay_var.get()),
                "interval": float(self.interval.get()),
                "intensity": float(self.intensity.get()),
                "mode": self.mode.get(),
                "device": self.device.get(),
            }
        )

    def _card(self, parent):
        wrap = tk.Frame(parent, bg=BORDER)
        inner = tk.Frame(wrap, bg=SURFACE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return wrap, inner

    def _build(self):
        self.root.title("GameReader")
        self.root.configure(bg=BG)
        self.root.geometry("860x740")
        self.root.minsize(760, 640)

        page = tk.Frame(self.root, bg=BG)
        page.pack(fill="both", expand=True, padx=22, pady=18)

        header = tk.Frame(page, bg=BG)
        header.pack(fill="x")
        title_col = tk.Frame(header, bg=BG)
        title_col.pack(side="left")
        tk.Label(title_col, text="GameReader", fg=TEXT, bg=BG, font=ui_font(28, "bold")).pack(anchor="w")
        tk.Label(
            title_col,
            text="Lektor z napisów albo tłumaczenie na żywo z dźwięku.",
            fg=MUTED,
            bg=BG,
            font=ui_font(13),
        ).pack(anchor="w", pady=(2, 0))
        self.live = tk.Label(header, text="  WYŁĄCZONY  ", fg=MUTED, bg=SURFACE2, font=ui_font(11, "bold"))
        self.live.pack(side="right", pady=8)

        modes = tk.Frame(page, bg=BG)
        modes.pack(fill="x", pady=(16, 0))
        saved_mode = self.cfg.get("mode", "auto")
        if saved_mode == "ocr":
            saved_mode = "auto"
        self.mode = tk.StringVar(value=saved_mode)
        self.btn_mode_ocr = PillButton(modes, "Napisy + dźwięk", lambda: self._set_mode("auto"), primary=True, width=180)
        self.btn_mode_audio = PillButton(modes, "Tylko dźwięk", lambda: self._set_mode("audio"), width=180)
        self.btn_mode_ocr.pack(side="left")
        self.btn_mode_audio.pack(side="left", padx=8)
        names = [PS_REMOTE] + [name for _i, name in self.devices]
        saved = self.cfg.get("device")
        self.device = tk.StringVar(value=saved if saved in names else PS_REMOTE)
        self.device_menu = tk.OptionMenu(modes, self.device, *names)
        self.device_menu.configure(bg=SURFACE2, fg=TEXT, highlightthickness=0, activebackground=SURFACE2, activeforeground=TEXT)
        self.device_menu.pack(side="right")
        tk.Label(modes, text="Wejście", fg=MUTED, bg=BG, font=ui_font(12)).pack(side="right", padx=(0, 6))

        steps = tk.Frame(page, bg=BG)
        steps.pack(fill="x", pady=(14, 10))
        self.btn_region = PillButton(steps, "1  Zaznacz napisy", self._pick_region, primary=True, width=180)
        self.btn_test = PillButton(steps, "2  Test", self._test_capture, width=110)
        self.btn_start = PillButton(steps, "3  Czytaj", self._toggle_run, width=140)
        self.btn_region.pack(side="left")
        self.btn_test.pack(side="left", padx=8)
        self.btn_start.pack(side="left")

        self.status = tk.StringVar(value="Tryb dźwięku: nasłuchuje dialog i tłumaczy na żywo. Głos: lokalny lektor Jarvis.")
        tk.Label(page, textvariable=self.status, fg=GOLD, bg=BG, font=ui_font(12), wraplength=800, justify="left").pack(
            anchor="w", pady=(4, 12)
        )

        preview_card, preview_inner = self._card(page)
        preview_card.pack(fill="x")
        tk.Label(preview_inner, text="PODGLĄD NAPISÓW", fg=MUTED, bg=SURFACE, font=ui_font(10, "bold")).pack(
            anchor="w", padx=14, pady=(10, 0)
        )
        self.preview = tk.Label(
            preview_inner,
            text="Napisy: podgląd zrzutu.  Dźwięk: tu pojawi się usłyszany tekst.",
            fg=MUTED,
            bg="#0F131A",
            font=ui_font(13),
            height=4,
        )
        self.preview.pack(fill="x", padx=12, pady=12)
        self.heard_var = tk.StringVar(value="")
        self.heard_lbl = tk.Label(
            preview_inner,
            textvariable=self.heard_var,
            fg=TEXT,
            bg="#0F131A",
            font=ui_font(13),
            wraplength=780,
            justify="left",
            anchor="w",
        )
        self.heard_lbl.pack(fill="x", padx=12, pady=(0, 12))

        line_card, line_inner = self._card(page)
        line_card.pack(fill="both", expand=True, pady=(12, 0))
        top_line = tk.Frame(line_inner, bg=SURFACE)
        top_line.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(top_line, text="LEKTOR", fg=MUTED, bg=SURFACE, font=ui_font(10, "bold")).pack(side="left")
        self.mood_lbl = tk.Label(top_line, text="emocja: —", fg=GOLD, bg=SURFACE, font=ui_font(11, "bold"))
        self.mood_lbl.pack(side="right")
        self.line_box = tk.Text(
            line_inner,
            wrap="word",
            bg=SURFACE,
            fg=ACCENT,
            insertbackground=ACCENT,
            relief="flat",
            font=ui_font(18, "bold"),
            height=6,
        )
        self.line_box.pack(fill="both", expand=True, padx=12, pady=10)

        bottom = tk.Frame(page, bg=BG)
        bottom.pack(fill="x", pady=(14, 0))
        self.overlay_var = tk.BooleanVar(value=bool(self.cfg.get("overlay", True)))
        Toggle(bottom, "Nakładka", self.overlay_var).pack(side="left")
        tk.Label(bottom, text="Głośność", fg=MUTED, bg=BG, font=ui_font(12)).pack(side="left", padx=(18, 6))
        self.intensity = tk.DoubleVar(value=float(self.cfg.get("intensity", 1.0)))
        tk.Scale(
            bottom,
            from_=0.6,
            to=1.6,
            resolution=0.1,
            orient="horizontal",
            variable=self.intensity,
            showvalue=0,
            length=140,
            bg=BG,
            fg=TEXT,
            troughcolor=SURFACE2,
            highlightthickness=0,
            activebackground=ACCENT,
        ).pack(side="left")
        tk.Label(bottom, text="Skan", fg=MUTED, bg=BG, font=ui_font(12)).pack(side="left", padx=(16, 6))
        self.interval = tk.DoubleVar(value=float(self.cfg.get("interval", DEFAULT_INTERVAL)))
        tk.Scale(
            bottom,
            from_=0.3,
            to=2.0,
            resolution=0.1,
            orient="horizontal",
            variable=self.interval,
            showvalue=0,
            length=100,
            bg=BG,
            fg=TEXT,
            troughcolor=SURFACE2,
            highlightthickness=0,
            activebackground=ACCENT,
        ).pack(side="left")

        perm = tk.Frame(page, bg=BG)
        perm.pack(fill="x", pady=(16, 0))
        PillButton(perm, "Ekran", open_screen_settings, width=120).pack(side="left")
        PillButton(perm, "Mikrofon", open_mic_settings, width=130).pack(side="left", padx=8)
        tk.Label(perm, text="Słucha PS Remote Play. Wejście zostaw na „PS Remote Play”.", fg=MUTED, bg=BG, font=ui_font(11)).pack(
            side="left", padx=8
        )

        for var in (self.overlay_var, self.interval, self.intensity, self.mode, self.device):
            var.trace_add("write", lambda *_: self._persist())
        self._set_mode(self.mode.get())

    def _set_mode(self, mode):
        if mode == "ocr":
            mode = "auto"
        self.mode.set(mode)
        auto = mode != "audio"
        self.btn_mode_ocr.primary = auto
        self.btn_mode_audio.primary = not auto
        self.btn_mode_ocr._draw(False)
        self.btn_mode_audio._draw(False)
        if auto:
            self.status.set("Auto: dźwięk z PS, a jak widać napisy — one mają pierwszeństwo.")
        else:
            self.status.set("Tylko dźwięk z PS Remote Play. Lektor nie patrzy na ekran.")

    def _restore(self):
        if self.region:
            _l, _t, width, height = self.region
            self.status.set(f"Pasek {width}×{height}. Czytaj: dźwięk, napisy pierwsze.")
            return
        self.status.set("Czytaj z dźwięku, albo zaznacz pasek napisów — wtedy one mają pierwszeństwo.")

    def _set_live(self, on):
        if on:
            self.live.configure(text="  CZYTA  ", fg="#08211B", bg=ACCENT)
            self.btn_start.set_text("Stop")
        else:
            self.live.configure(text="  WYŁĄCZONY  ", fg=MUTED, bg=SURFACE2)
            self.btn_start.set_text("3  Czytaj")

    def _set_line(self, text, mood=None):
        self.line_box.delete("1.0", "end")
        self.line_box.insert("1.0", text)
        if mood:
            self.mood_lbl.configure(text=f"emocja: {MOODS[mood]['label']}")

    def _pick_region(self):
        self._stop()
        self._set_mode("auto")
        hidden = False
        if self.overlay is not None:
            self.overlay.withdraw()
            hidden = True
        self.status.set("Chowam okna — przeciągnij pasek napisów na grze. Esc anuluje.")
        self.root.update()
        self.root.withdraw()
        self.root.update()
        time.sleep(0.25)
        region = None
        try:
            if game_reader_bin() is not None:
                region = pick_region_native()
            else:
                region = RegionSelector(self.root).pick()
        except Exception as exc:
            self.status.set(f"Nie dało się zaznaczyć: {exc}")
            region = None
        finally:
            try:
                self.root.deiconify()
                self.root.lift()
            except tk.TclError:
                pass
            if hidden and self.overlay is not None:
                self.overlay.deiconify()
        self.region = region
        self._persist()
        if region is None:
            self.status.set("Nie zaznaczono obszaru.")
            return
        _l, _t, width, height = region
        try:
            frame = self._capture_region()
            self._show_preview(frame)
            text = self.ocr.read(frame)
            if text:
                self.status.set(f"Obszar {width}×{height}: {text[:90]}")
            else:
                self.status.set(f"Obszar {width}×{height}. Jak pojawi się napis, kliknij Test.")
        except Exception as exc:
            self.status.set(f"Mam obszar {width}×{height}, ale zrzut nie wszedł: {exc}")

    def _ensure_overlay(self):
        if self.overlay_var.get():
            if self.overlay is None:
                self.overlay = OverlayWindow(self.root)
        elif self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def _toggle_run(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        if self.running:
            return
        if self.mode.get() == "audio":
            target = self._audio_loop
            self.status.set("Podpinam dźwięk PS Remote Play…")
        elif self.region is None:
            target = self._audio_loop
            self.status.set("Brak paska napisów — czytam z dźwięku. Zaznacz napisy, żeby miały pierwszeństwo.")
        else:
            target = self._hybrid_loop
            self.status.set("Dźwięk z PS, napisy z ekranu mają pierwszeństwo.")
        self.running = True
        self.last_key = ""
        self.subtitle_until = 0.0
        self._ensure_overlay()
        self._set_live(True)
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _stop(self):
        self.running = False
        if self.transcriber is not None:
            self.transcriber.stop()
            self.transcriber = None
        self.lektor.stop()
        self._set_live(False)
        self.status.set("Zatrzymane.")

    def _quit(self):
        self.running = False
        if self.transcriber is not None:
            self.transcriber.stop()
            self.transcriber = None
        self.lektor.stop()
        self._persist()
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        self.root.destroy()

    def _capture_region(self):
        if self.region is None:
            raise RuntimeError("Brak obszaru.")
        left, top, width, height = self.region
        path = os.path.join(tempfile.gettempdir(), f"gamereader_cap_{os.getpid()}_{time.time_ns()}.png")
        if self.overlay is not None:
            self.root.after(0, self.overlay.withdraw)
            time.sleep(0.05)
        try:
            error = None
            if helper_available():
                try:
                    line, sock, _rest = helper_call(f"SHOT {left} {top} {width} {height} {path}", timeout=8)
                    sock.close()
                    if line.startswith("OK") and os.path.exists(path):
                        return self._load_capture(path)
                    error = line or "helper shot"
                except OSError as exc:
                    error = str(exc)
            bin_path = game_reader_bin()
            if bin_path is not None:
                result = subprocess.run(
                    [str(bin_path), "--shot", str(left), str(top), str(width), str(height), path],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and os.path.exists(path):
                    return self._load_capture(path)
                error = (result.stderr or result.stdout or error or "zrzut nie wszedł").strip()
            if error and any(word in error.lower() for word in ("zgody", "ekranu", "tcc", "denied")):
                raise PermissionError(error)
            raise RuntimeError(error or "zrzut nie wszedł")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
            if self.overlay is not None and self.overlay_var.get():
                self.root.after(0, self.overlay.deiconify)

    def _load_capture(self, path):
        with Image.open(path) as image:
            image.load()
            return np.array(image.convert("RGB"))[:, :, ::-1].copy()

    def _speak_line(self, src, translate=False):
        if self.running:
            self.line_q.put((src, translate))
            return
        try:
            text = self.translator.translate(src) if translate else src
            if not text:
                return
            mood = self.lektor.speak(text, volume=float(self.intensity.get()))
            self.events.put(("line", text, mood))
        except Exception as exc:
            self.events.put(("status", f"Błąd głosu: {exc}"))

    def _tts_loop(self):
        prepared = None
        while True:
            if prepared is None:
                try:
                    src, translate = self.line_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    prepared = self._prepare_line(src, translate)
                except Exception as exc:
                    self.events.put(("status", f"Błąd głosu: {exc}"))
                    prepared = None
                    continue
                if prepared is None:
                    continue
            text, mood, path = prepared
            prepared = None
            try:
                self.lektor.stop()
                self.lektor._play_file(path)
                self.events.put(("line", text, mood))
                try:
                    nxt_src, nxt_tr = self.line_q.get(timeout=0.05)
                except queue.Empty:
                    nxt_src = None
                if nxt_src is not None:
                    try:
                        prepared = self._prepare_line(nxt_src, nxt_tr)
                    except Exception as exc:
                        self.events.put(("status", f"Błąd głosu: {exc}"))
                self.lektor.wait()
            except Exception as exc:
                self.events.put(("status", f"Błąd głosu: {exc}"))

    def _prepare_line(self, src, translate):
        text = self.translator.translate(src) if translate else src
        text = normalize_text(text)
        if not text:
            return None
        source = src if translate else None
        mood, path = self.lektor.prepare(text, source=source, volume=float(self.intensity.get()))
        return text, mood, path

    def _on_draft(self, text):
        self.events.put(("heard", text))

    def _on_heard(self, heard):
        if time.monotonic() < self.subtitle_until:
            return
        if not heard or text_key(heard) == self.last_key:
            return
        if heard.lower() in JUNK_HEARD:
            return
        self.last_key = text_key(heard)
        self.events.put(("heard", heard))
        self.line_q.put((heard, True))

    def _flush_line_q(self):
        try:
            while True:
                self.line_q.get_nowait()
        except queue.Empty:
            pass

    def _on_subtitle(self, src):
        if not usable_ocr(src):
            return
        self.subtitle_until = time.monotonic() + 2.8
        key = text_key(src)
        if key == self.last_key:
            return
        self.last_key = key
        self._flush_line_q()
        self.lektor.stop()
        self.events.put(("status", "Napisy — pierwszeństwo przed dźwiękiem."))
        self.line_q.put((src, should_translate(src)))

    def _hybrid_loop(self):
        scanner = threading.Thread(target=self._ocr_scan_loop, daemon=True)
        scanner.start()
        self._audio_loop()

    def _ocr_scan_loop(self):
        last_hash = ""
        while self.running:
            started = time.time()
            try:
                frame = self._capture_region()
                digest = hashlib.sha1(frame.tobytes()).hexdigest()[:20] if frame is not None else ""
                self.events.put(("preview", frame))
                if digest and digest != last_hash:
                    last_hash = digest
                    src = self.ocr.read(frame)
                    if src:
                        self._on_subtitle(src)
            except Exception as exc:
                self.events.put(("status", f"Napisy: {exc}"))
            wait = max(0.28, float(self.interval.get())) - (time.time() - started)
            if wait > 0:
                time.sleep(wait)

    def _device_index(self):
        chosen = self.device.get()
        for index, name in self.devices:
            if name == chosen:
                return index
        return None

    def _ingest_live(self, mono, state, live):
        voiced, quiet, spoken = state
        rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
        duration = mono.size / float(SAMPLE_RATE)
        if rms >= SPEECH_RMS:
            voiced = True
            quiet = 0.0
            spoken += duration
            live.feed(mono)
        elif voiced:
            quiet += duration
        if voiced and spoken >= MIN_SPEECH_SEC and (quiet >= SILENCE_SEC or spoken >= MAX_SPEECH_SEC):
            live.flush()
            return [False, 0.0, 0.0]
        return [voiced, quiet, spoken]

    def _audio_loop(self):
        try:
            self.events.put(("status", "Rozgrzewam lektor i tłumacz…"))
            self.translator.ensure()
            self.translator.translate("Stay here.")
            self.lektor.ensure()
            self.lektor.prepare("Zostań tutaj.", volume=1.0)
        except Exception as exc:
            self.events.put(("status", f"Błąd silnika dźwięku: {exc}"))
            self.running = False
            self.events.put(("stopped", None))
            return

        self.transcriber = LiveTranscriber(
            self.stt,
            self._on_heard,
            on_draft=self._on_draft,
            on_error=lambda msg: self.events.put(("status", f"Błąd STT: {msg}")),
            on_ready=lambda: self.events.put(("status", "Parakeet gotowy — słucham w locie.")),
        )
        self.transcriber.start()
        try:
            if self.device.get() == PS_REMOTE:
                self._audio_loop_ps_remote()
            else:
                self._audio_loop_microphone()
        finally:
            if self.transcriber is not None:
                self.transcriber.stop()
                self.transcriber = None
        self.events.put(("stopped", None))

    def _audio_loop_ps_remote(self):
        tap = GameAudioTap()
        live = self.transcriber
        try:
            tap.start()
            heard_from = tap.message.replace("LISTENING ", "") if tap.message.startswith("LISTENING") else "PS Remote Play"
            self.events.put(("status", f"Słucham {heard_from} · lektor czyta w locie."))
            state = [False, 0.0, 0.0]
            while self.running:
                chunk = tap.read(0.03)
                if chunk is None:
                    dead = (tap.proc is not None and tap.proc.poll() is not None) or (
                        tap.sock is not None and not tap.alive
                    )
                    if dead:
                        raise RuntimeError(tap.message or "PS Remote Play się rozłączył.")
                    continue
                state = self._ingest_live(chunk, state, live)
        except PermissionError as exc:
            self.events.put(("perm", str(exc)))
        except Exception as exc:
            text = str(exc)
            if any(word in text.lower() for word in ("tcc", "zgody", "przechwytywania", "denied", "not permitted")):
                self.events.put(("perm", text))
            else:
                self.events.put(("status", f"Błąd PS Remote: {exc}"))
            self.running = False
        finally:
            tap.stop()

    def _audio_loop_microphone(self):
        import sounddevice as sd

        self.events.put(("status", "Słucham mikrofonu, nie dźwięku z PS. Wejście ustaw na PS Remote Play."))
        state = [False, 0.0, 0.0]
        live = self.transcriber
        block = 0.03
        pending = queue.Queue()

        def callback(indata, _frames, _time_info, _status):
            if not self.running:
                raise sd.CallbackStop
            pending.put(indata[:, 0].copy())

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=int(SAMPLE_RATE * block),
                device=self._device_index(),
                callback=callback,
            ):
                while self.running:
                    try:
                        chunk = pending.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    state[:] = self._ingest_live(chunk, state, live)
        except Exception as exc:
            self.events.put(("status", f"Błąd nasłuchu: {exc}"))
            self.running = False

    def _test_capture(self):
        if self.region is None:
            self.status.set("Najpierw zaznacz pasek napisów.")
            return
        self._set_mode("auto")
        self.status.set("Robię test napisów…")

        def work():
            try:
                frame = self._capture_region()
                self.events.put(("preview", frame))
                if is_black_frame(frame):
                    if helper_has_screen() is False:
                        self.events.put(("perm", None))
                    else:
                        self.events.put(("status", "Zrzut jest ciemny. Zaznacz ciaśniejszy pasek napisów."))
                    return
                src = self.ocr.read(frame)
                if src:
                    self._speak_line(src, translate=should_translate(src))
                    self.events.put(("status", f"Test OK: {src[:90]}"))
                else:
                    self.events.put(("status", "Widzę kadr, ale nie czytam tekstu. Zaznacz sam pasek napisu, nie całą grę."))
            except Exception as exc:
                self.events.put(("status", f"Błąd: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _loop(self):
        empty = 0
        self.events.put(("status", "Lektor słucha napisów."))
        while self.running:
            started = time.time()
            try:
                frame = self._capture_region()
                self.events.put(("preview", frame))
                if is_black_frame(frame):
                    if helper_has_screen() is False:
                        self.events.put(("perm", None))
                        self.running = False
                        break
                    empty += 1
                    if empty == 3:
                        self.events.put(("status", "Zrzut jest ciemny. Zaznacz pasek jeszcze raz."))
                    continue
                src = self.ocr.read(frame)
                if src and text_key(src) != self.last_key:
                    self.last_key = text_key(src)
                    empty = 0
                    self._speak_line(src)
                elif not src:
                    empty += 1
                    if empty == 4:
                        self.events.put(("status", "Widzę ekran, ale nie ma napisu."))
            except Exception as exc:
                self.events.put(("status", f"Błąd: {exc}"))
                self.running = False
                break
            wait = max(MIN_INTERVAL, float(self.interval.get())) - (time.time() - started)
            if wait > 0:
                time.sleep(wait)
        self.events.put(("stopped", None))

    def _show_preview(self, frame):
        if frame is None or frame.size == 0:
            return
        from PIL import ImageEnhance, ImageOps

        image = Image.fromarray(np.ascontiguousarray(frame[:, :, ::-1]))
        image = ImageOps.autocontrast(ImageEnhance.Brightness(image).enhance(1.35))
        image.thumbnail((PREVIEW_W, PREVIEW_H))
        photo = ImageTk.PhotoImage(image)
        self.preview_photo = photo
        self.preview.configure(image="", text="")
        self.preview.configure(image=photo, text="")
        self.preview.image = photo

    def _drain(self):
        try:
            while True:
                item = self.events.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status.set(item[1])
                elif kind == "stopped":
                    self._set_live(False)
                elif kind == "perm":
                    self._set_live(False)
                    extra = item[1] if len(item) > 1 and item[1] else ""
                    self.status.set(
                        "Włącz Nagrywanie ekranu dla GameReader i zrestartuj aplikację. "
                        "Bez tego nie słychać PS Remote Play. " + extra
                    )
                    open_screen_settings()
                elif kind == "mic":
                    self._set_live(False)
                    self.status.set("Brak dostępu do mikrofonu — włącz go dla GameReader.")
                    open_mic_settings()
                elif kind == "heard":
                    self.heard_var.set(item[1])
                elif kind == "preview":
                    self._show_preview(item[1])
                elif kind == "line":
                    _, src, mood = item
                    self._set_line(src, mood)
                    self._ensure_overlay()
                    if self.overlay is not None:
                        self.overlay.set_text(src)
                    self.status.set(f"Czyta lektor · {MOODS[mood]['label']}")
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain)


def main():
    set_mac_app_name()
    root = tk.Tk()
    hide_helper_dock_icon()
    GameReaderApp(root)
    hide_helper_dock_icon()
    root.after(80, hide_helper_dock_icon)
    root.after(400, hide_helper_dock_icon)
    root.mainloop()


if __name__ == "__main__":
    main()
