"""„Open Jev” dla lektora: mały otwarty model podejmuje szybkie decyzje o napisach.

Technika System One (jak TypeSafe Jev i open-alternative-jev): model NIE generuje tekstu — czyta napis
raz i odpowiada na typowane pytanie rozkładem prawdopodobieństwa po podanych opcjach (logity liter A/B
z jednego przebiegu). Qwen3.5-4B (4 bit, MLX na GPU Maca): ok. 55–75 ms na pytanie na M5 Pro, a lektor
(Supertonic) liczy na CPU, więc sobie nie przeszkadzają.

Pytania (zmierzone na napisach z logu lektora):
- dialog: czy to kwestia postaci, a nie menu / podpowiedź przycisku / znak wodny / powiadomienie
  z pulpitu / zlepek liter — AUROC 1,00 na próbce, śmieci z całego logu dostają p ≤ 0,15;
- informacja: czy widz dowie się z kwestii czegoś nowego (a nie sam okrzyk czy potwierdzenie) —
  AUROC 0,99, 91 % trafnie; w zrywie najpierw wypadają kwestie bez informacji.
Odrzucone po pomiarach: Laya-multilingual (Jev-podobny, 100+ języków) zgadywał na polskich napisach
z gier (5–7/15); emocje z samego tekstu 9–10/15 (zwykłe kwestie brał za szept albo krzyk) — emocje
bierzemy z głosu postaci w dźwięku gry.

Zasoby: jeden model (~3 GB pamięci, tylko Mac z ≥16 GB), pytania wyłącznie o nowe napisy, kolejka bez
zaległości (stare zlecenia przepadają), limit pamięci podręcznej MLX, a po 10 min bez pracy model jest
zwalniany z pamięci. Nigdy nie wstrzymuje lektora: dopóki się ładuje (pierwszy raz pobiera ~3 GB) albo
go nie ma (Windows, mało pamięci), decydują same reguły w silniku.
"""
import gc
import importlib.util
import os
import platform
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque

BRAIN_MODEL = "mlx-community/Qwen3.5-4B-4bit"
BRAIN_MIN_RAM_GB = 16
# zlecenie starsze niż tyle sekund jest już nieaktualne (napis zniknął) — przepada
BRAIN_JOB_TTL = 6.0
BRAIN_QUEUE_MAX = 8
# po tylu sekundach bez nowych napisów model zwalnia pamięć; wraca przy następnym napisie
BRAIN_IDLE_UNLOAD_SEC = 600.0
# pamięć podręczna buforów MLX (bez limitu potrafi rosnąć do kilku GB)
BRAIN_MLX_CACHE_BYTES = 256 * 1024 * 1024

DIALOGUE_QUESTION = (
    "To tekst odczytany przez OCR z ekranu gry albo filmu. Czy to kwestia dialogowa wypowiedziana przez postać?",
    ["tak — zdanie mówione przez postać", "nie — menu, podpowiedź przycisku, logo, znak wodny, napisy końcowe albo zlepek liter"],
)
INFO_QUESTION = (
    "Czy z tej kwestii widz dowie się czegoś nowego (fakt, plan, pytanie, polecenie, kto co zrobił)?",
    ["nie — to tylko okrzyk, reakcja albo potwierdzenie", "tak — jest w niej informacja"],
)
# o informację pytamy tylko, gdy to (chyba) dialog — śmieci kosztują jeden przebieg
INFO_MIN_DIALOGUE = 0.10


def _ram_gb():
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2).stdout
        return int(out.strip()) / 1024**3
    except Exception:
        return 0.0


def brain_supported():
    """Mac z Apple Silicon, dość pamięci i biblioteka mlx-lm w silniku."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    if os.environ.get("LIVEDUB_NO_BRAIN"):
        return False
    if importlib.util.find_spec("mlx_lm") is None:
        return False
    return _ram_gb() >= BRAIN_MIN_RAM_GB


class LektorBrain:
    """Pytania do modelu w tle; wyniki w pamięci podręcznej: tekst → {"dialog": p, "info": p | None}.

    MLX trzyma obliczenia per wątek, więc wczytanie i wszystkie pytania idą przez jeden stały wątek."""

    def __init__(self, log=None):
        self._log = log or (lambda _line: None)
        self._jobs = deque()
        self._results = OrderedDict()
        self._pending = set()
        self._cond = threading.Condition()
        self._thread = None
        self.ready = False
        self.failed = None
        self.model = None
        self.tok = None
        self._letters = {}

    def start(self):
        with self._cond:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="mlx-brain", daemon=True)
                self._thread.start()

    # --- API dla silnika -----------------------------------------------------------------
    def check(self, text):
        """Zleć ocenę napisu (od razu wraca). Wynik odbierz przez verdict()."""
        if not text or self.failed:
            return
        with self._cond:
            if text in self._results or text in self._pending:
                return
            self._pending.add(text)
            self._jobs.append((time.monotonic(), text))
            while len(self._jobs) > BRAIN_QUEUE_MAX:
                _t, old = self._jobs.popleft()
                self._pending.discard(old)
            self._cond.notify_all()

    def verdict(self, text, wait=0.0):
        """{"dialog": p, "info": p | None} albo None, gdy modelu nie ma albo wynik nie zdążył w `wait` s."""
        deadline = time.monotonic() + max(0.0, wait)
        with self._cond:
            while True:
                if text in self._results:
                    return self._results[text]
                left = deadline - time.monotonic()
                if left <= 0 or text not in self._pending or not self.ready:
                    return None
                self._cond.wait(left)

    # --- wątek modelu --------------------------------------------------------------------
    def _run(self):
        try:
            import mlx.core as mx

            mx.set_default_device(mx.gpu)
            mx.set_cache_limit(BRAIN_MLX_CACHE_BYTES)
        except Exception:
            pass
        if not self._load_logged():
            return
        while True:
            with self._cond:
                while not self._jobs:
                    idle = not self._cond.wait(BRAIN_IDLE_UNLOAD_SEC)
                    if idle and not self._jobs and self.model is not None:
                        self._unload()
                queued_at, text = self._jobs.popleft()
            if time.monotonic() - queued_at > BRAIN_JOB_TTL:
                with self._cond:
                    self._pending.discard(text)
                    self._cond.notify_all()
                continue
            if self.model is None and not self._load_logged():
                return
            result = None
            try:
                t0 = time.monotonic()
                result = {"dialog": self._ask(text, *DIALOGUE_QUESTION)[0], "info": None}
                if result["dialog"] >= INFO_MIN_DIALOGUE:
                    result["info"] = self._ask(text, *INFO_QUESTION)[1]
                ms = (time.monotonic() - t0) * 1000
                if ms > 400:
                    self._log(f"model decyzji wolny: {ms:.0f} ms")
            except Exception as exc:
                self._log(f"model decyzji: błąd {exc}")
            with self._cond:
                self._pending.discard(text)
                if result is not None:
                    self._results[text] = result
                    while len(self._results) > 512:
                        self._results.popitem(last=False)
                self._cond.notify_all()

    def _load_logged(self):
        try:
            t0 = time.monotonic()
            self._load()
            self.ready = True
            self._log(f"model decyzji gotowy ({BRAIN_MODEL.split('/')[-1]}, {time.monotonic() - t0:.1f}s)")
            return True
        except Exception as exc:
            self.failed = str(exc)
            self.ready = False
            self._log(f"model decyzji niedostępny: {exc}")
            with self._cond:
                self._jobs.clear()
                self._pending.clear()
                self._cond.notify_all()
            return False

    def _load(self):
        import mlx.core as mx
        from huggingface_hub import snapshot_download
        from mlx_lm import load

        try:
            path = snapshot_download(BRAIN_MODEL, local_files_only=True)
        except Exception:
            self._log(f"model decyzji: pobieram {BRAIN_MODEL} (~3 GB, jednorazowo)")
            path = snapshot_download(BRAIN_MODEL)
        model, tok = load(path)
        # wagi do pamięci od razu — leniwe tablice MLX nie mogą czekać na inny wątek
        mx.eval(model.parameters())
        self.model, self.tok = model, tok
        self._letters = {}
        self._ask("Rozgrzewka.", *DIALOGUE_QUESTION)

    def _unload(self):
        self.model = None
        self.tok = None
        self.ready = False
        gc.collect()
        try:
            import mlx.core as mx

            mx.clear_cache()
        except Exception:
            pass
        self._log("model decyzji: zwalniam pamięć (brak napisów)")

    def _letter_id(self, letter):
        if letter not in self._letters:
            self._letters[letter] = self.tok.encode(letter, add_special_tokens=False)[-1]
        return self._letters[letter]

    def _ask(self, state, question, options):
        """Typowane pytanie → prawdopodobieństwa opcji (w podanej kolejności), jeden przebieg."""
        import mlx.core as mx

        labels = "ABCDEFG"[: len(options)]
        body = (
            f"Tekst: «{state}»\n\n{question}\n"
            + "\n".join(f"{label}) {option}" for label, option in zip(labels, options))
            + "\nOdpowiedz jedną literą."
        )
        msgs = [{"role": "user", "content": body}]
        try:
            prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        ids = mx.array(self.tok.encode(prompt, add_special_tokens=False))[None]
        logits = self.model(ids)[0, -1]
        picked = logits[mx.array([self._letter_id(label) for label in labels])].astype(mx.float32)
        return [float(p) for p in mx.softmax(picked).tolist()]
