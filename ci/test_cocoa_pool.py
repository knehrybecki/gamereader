"""Zrzuty okna i lista okien w wątkach Pythona nie zostawiają obiektów Cocoa w pamięci.

17.11: bez autorelease pool każdy zrzut okna (5 MB klatka Chrome 1512×874) zostawał na zawsze — silnik urósł
do 8 GB w kwadrans."""
import os
import subprocess
import sys
import threading
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


def footprint_mb():
    out = subprocess.run(["footprint", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    line = next(line for line in out.splitlines() if "Footprint:" in line)
    value, unit = line.split("Footprint:")[1].split()[:2]
    return float(value) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[unit]


def png_of_frame():
    """Ta sama droga obiektów Cocoa co w capture_remote_play_band (krok po kroku), na sztucznej klatce."""
    import numpy as np
    from Cocoa import NSBitmapImageFileTypePNG, NSBitmapImageRep
    from PIL import Image
    from Quartz import (CGBitmapContextCreate, CGBitmapContextCreateImage, CGColorSpaceCreateDeviceRGB,
                        CGImageCreateWithImageInRect, CGRectMake, kCGImageAlphaPremultipliedFirst)

    ctx = CGBitmapContextCreate(None, 1512, 874, 8, 0, CGColorSpaceCreateDeviceRGB(), kCGImageAlphaPremultipliedFirst)
    full = CGBitmapContextCreateImage(ctx)
    piece = CGImageCreateWithImageInRect(full, CGRectMake(0, 640, 1512, 230))
    rep = NSBitmapImageRep.alloc().initWithCGImage_(piece)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    image = Image.open(BytesIO(bytes(data))).convert("RGB")
    return np.ascontiguousarray(np.array(image)[:, :, ::-1])


def growth_in_thread(pooled, n=150):
    """Przyrost pamięci (MB) po n klatkach w jednym wątku, w świeżym procesie."""
    out = subprocess.run([sys.executable, __file__, "--probe", "pool" if pooled else "bare", str(n)],
                         capture_output=True, text=True, timeout=300)
    return float(out.stdout.strip().splitlines()[-1])


def probe(mode, n):
    fn = ge.cocoa_pool(png_of_frame) if mode == "pool" else png_of_frame
    growth = []

    def work():
        before = footprint_mb()
        for _ in range(n):
            fn()
        # pomiar jeszcze w wątku: koniec wątku opróżnia jego obiekty autoreleased, a pętla OCR silnika się nie kończy
        growth.append(footprint_mb() - before)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    print(growth[0])


@unittest.skipUnless(sys.platform == "darwin", "Cocoa tylko na macOS")
class CocoaPoolTest(unittest.TestCase):
    def test_pooled_frame_capture_keeps_memory_flat(self):
        # kontrola: bez puli ~1 MB na klatkę — inaczej ten test niczego nie mierzy
        self.assertGreater(growth_in_thread(pooled=False), 80)
        self.assertLess(growth_in_thread(pooled=True), 40)

    def test_window_capture_and_lookup_run_in_a_pool(self):
        for fn in (ge.capture_remote_play_band, ge._quartz_remote_play_info, ge._quartz_chrome_info,
                   ge._remote_play_pids, ge.remote_play_bounds):
            self.assertTrue(getattr(fn, "_cocoa_pool", False), fn.__name__)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--probe"]:
        probe(sys.argv[2], int(sys.argv[3]))
    else:
        unittest.main()
