"""LiveDub na Windowsie: okna źródła (PS Remote Play / Chrome), zrzut paska napisów i OCR.

OCR to wbudowany silnik Windowsa (Windows.Media.Ocr, przez pywinrt) — czyta polskie znaki,
o ile w systemie jest język polski (Ustawienia → Czas i język → Język i region → Dodaj język).
Współrzędne są w fizycznych pikselach ekranu (proces jest „DPI aware”), tak jak zrzut mss.
"""
import ctypes
import os
import threading
from ctypes import wintypes

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:
    dwmapi = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

REMOTE_PLAY_EXES = ("remoteplay.exe",)
BROWSER_EXES = ("chrome.exe", "msedge.exe", "brave.exe")
BROWSER_CLASS = "Chrome_WidgetWin_1"

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


def enable_dpi_awareness():
    """Fizyczne piksele zamiast przeskalowanych — inaczej przy skalowaniu 125–150 % pasek się rozjeżdża."""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


_exe_cache = {}


def _process_exe(pid):
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    if len(_exe_cache) > 512:
        _exe_cache.clear()
    _exe_cache[pid] = name
    return name


def _window_rect(hwnd):
    rect = wintypes.RECT()
    # bez niewidocznej ramki (cienia) Windows 10/11 — sama zawartość okna
    if dwmapi is not None:
        try:
            if dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
            ) == 0:
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except OSError:
            pass
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return None


def _cloaked(hwnd):
    """Okno „schowane” przez system (inny pulpit wirtualny, aplikacja UWP w tle)."""
    if dwmapi is None:
        return False
    value = wintypes.DWORD(0)
    try:
        if dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) == 0:
            return value.value != 0
    except OSError:
        pass
    return False


def list_windows():
    """Widoczne okna najwyższego poziomu: [{hwnd, title, cls, exe, rect}] (od góry stosu okien)."""
    found = []

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or _cloaked(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buf, length + 1)
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = _window_rect(hwnd)
            if rect is None:
                return True
            found.append(
                {
                    "hwnd": int(hwnd or 0),
                    "title": title_buf.value,
                    "cls": cls_buf.value,
                    "exe": _process_exe(pid.value),
                    "rect": rect,
                }
            )
        except Exception:
            pass
        return True

    user32.EnumWindows(_EnumProc(callback), 0)
    return found


def find_remote_play():
    """(x, y, w, h, hwnd) największego okna PS Remote Play albo None."""
    best = None
    best_area = 0
    for win in list_windows():
        title = win["title"].lower()
        if win["exe"] not in REMOTE_PLAY_EXES and "ps remote play" not in title:
            continue
        x, y, w, h = win["rect"]
        if w < 200 or h < 140:
            continue
        if w * h > best_area:
            best_area = w * h
            best = (x, y, w, h, win["hwnd"])
    return best


def find_browser(video_only=False, video_titles=()):
    """(x, y, w, h, hwnd, tytuł) okna Chrome/Edge: najpierw karta z wideo, potem największe."""
    best = None
    best_score = 0.0
    for win in list_windows():
        if win["exe"] not in BROWSER_EXES or win["cls"] != BROWSER_CLASS or not win["title"]:
            continue
        video = any(key in win["title"].lower() for key in video_titles)
        if video_only and not video:
            continue
        x, y, w, h = win["rect"]
        if w < 320 or h < 200:
            continue
        score = w * h * (4.0 if video else 1.0)
        if score > best_score:
            best_score = score
            best = (x, y, w, h, win["hwnd"], win["title"])
    return best


def primary_display_bounds():
    return (0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))


_grab_local = threading.local()


def grab(left, top, width, height):
    """Zrzut prostokąta ekranu (BGR, uint8) albo None. mss nie jest bezpieczne między wątkami."""
    try:
        import mss
    except Exception:
        return None
    sct = getattr(_grab_local, "sct", None)
    try:
        if sct is None:
            sct = (getattr(mss, "MSS", None) or mss.mss)()
            _grab_local.sct = sct
        shot = sct.grab({"left": int(left), "top": int(top), "width": int(width), "height": int(height)})
        arr = np.asarray(shot, dtype=np.uint8)
        if arr.ndim != 3 or arr.shape[0] < 4 or arr.shape[1] < 4:
            return None
        return np.ascontiguousarray(arr[:, :, :3])
    except Exception:
        _grab_local.sct = None
        return None


class WindowsOcrBackend:
    """Windows.Media.Ocr: jeden silnik na język. Zwraca wiersze jak Vision: (tekst, pewność, [x, y, w, h] w px)."""

    def __init__(self):
        self._engines = {}
        self._lock = threading.Lock()
        self.missing_polish = False
        self.error = ""
        try:
            # moduły WinRT ładujemy w głównym wątku — pozostałe wątki korzystają z tego samego MTA
            import winrt.windows.media.ocr  # noqa: F401
            import winrt.windows.globalization  # noqa: F401
            import winrt.windows.graphics.imaging  # noqa: F401
            import winrt.windows.storage.streams  # noqa: F401
        except Exception as exc:
            self.error = f"OCR Windows niedostępny: {exc}"

    def languages(self):
        try:
            from winrt.windows.media.ocr import OcrEngine

            return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
        except Exception:
            return []

    def _engine(self, tags):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        key = tuple(tags or ())
        with self._lock:
            if key in self._engines:
                return self._engines[key]
            engine = None
            for tag in list(tags or ()) + ["pl-PL", "pl"]:
                try:
                    lang = Language(tag)
                    if OcrEngine.is_language_supported(lang):
                        engine = OcrEngine.try_create_from_language(lang)
                        if engine is not None:
                            break
                except Exception:
                    continue
            if engine is None:
                # bez polskiego pakietu OCR — angielski/systemowy (bez ąęćłńóśźż)
                self.missing_polish = True
                engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                raise RuntimeError("Windows nie ma żadnego języka OCR.")
            self._engines[key] = engine
            return engine

    def recognize(self, image, tags=None):
        """image: PIL RGB. Zwraca [(tekst, 1.0, [x, y, w, h])] — współrzędne w pikselach obrazu."""
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        engine = self._engine(tags)
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        bgra = np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]])
        height, width = bgra.shape[:2]
        writer = DataWriter()
        writer.write_bytes(bgra.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height)
        result = engine.recognize_async(bitmap).get()
        rows = []
        for line in result.lines or []:
            text = str(line.text or "").strip()
            if not text:
                continue
            rects = [word.bounding_rect for word in (line.words or [])]
            if rects:
                x1 = min(r.x for r in rects)
                y1 = min(r.y for r in rects)
                x2 = max(r.x + r.width for r in rects)
                y2 = max(r.y + r.height for r in rects)
                box = [int(x1), int(y1), max(1, int(x2 - x1)), max(1, int(y2 - y1))]
            else:
                box = None
            rows.append((text, 1.0, box))
        return rows


def open_language_settings():
    try:
        os.startfile("ms-settings:regionlanguage")
    except OSError:
        pass
