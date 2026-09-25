#!/usr/bin/env python3
import json
import logging
import sys
import threading
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
        info["CFBundleName"] = "GameReader"
        info["CFBundleDisplayName"] = "GameReader"
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
except Exception:
    pass

from gamereader_engine import Engine


def emit(payload):
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
                engine.stop()
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
