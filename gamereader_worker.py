#!/usr/bin/env python3
import json
import os
import logging
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)
logging.getLogger("stanza").setLevel(logging.ERROR)
logging.getLogger("argostranslate").setLevel(logging.ERROR)

try:
    from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
    from Foundation import NSBundle

    info = NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info["LSUIElement"] = True
        info["CFBundleName"] = "LiveDub"
        info["CFBundleDisplayName"] = "LiveDub"
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
except Exception:
    pass

from gamereader_engine import Engine


if sys.platform == "win32":
    LOG_PATH = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LiveDub", "LiveDub.log")
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    # polskie znaki w komunikatach do Electrona — zawsze UTF-8, niezależnie od strony kodowej konsoli
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
else:
    LOG_PATH = os.path.expanduser("~/Library/Logs/LiveDub.log")


def log_event(payload):
    """Log diagnostyczny: co silnik widział, czytał i zgłaszał (bez podglądu obrazu)."""
    if payload.get("event") not in ("status", "line", "heard", "running", "perm", "debug"):
        return
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            stamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
            handle.write(f"{stamp} {payload.get('event')}: {payload.get('text', payload.get('on', ''))}\n")
    except OSError:
        pass


def emit(payload):
    log_event(payload)
    if payload.get("event") == "debug":
        return
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    try:
        engine = Engine(emit)
    except Exception as exc:
        emit({"event": "status", "text": f"Silnik nie wstaje: {exc}"})
        raise
    emit({"event": "ready", **engine.snapshot()})
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            emit({"event": "status", "text": "Zła komenda."})
            continue
        cmd = msg.get("cmd")
        try:
            if cmd == "start":
                engine.start()
            elif cmd == "stop":
                engine.stop(user=True)
            elif cmd == "pick":
                threading.Thread(target=engine.pick, daemon=True).start()
            elif cmd == "test":
                threading.Thread(target=engine.test, daemon=True).start()
            elif cmd == "config":
                engine.configure(msg)
            elif cmd == "save":
                engine.save_settings()
            elif cmd == "quit":
                engine.stop()
                break
        except Exception as exc:
            emit({"event": "status", "text": f"Błąd: {exc}"})


if __name__ == "__main__":
    main()
