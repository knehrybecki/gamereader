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

# ciężkie biblioteki (torch przez transformers/argostranslate) importowane naraz z dwóch wątków
# kończą się „deadlock detected by _ModuleLock('torch…')” — ładowanie modeli idzie po kolei
HEAVY_IMPORT_LOCK = threading.RLock()

BRAIN_MODEL = "mlx-community/Qwen3.5-4B-4bit"
BRAIN_MIN_RAM_GB = 16
# zlecenie starsze niż tyle sekund jest już nieaktualne (napis zniknął) — przepada
BRAIN_JOB_TTL = 6.0
BRAIN_QUEUE_MAX = 8
# po tylu sekundach bez nowych napisów model zwalnia pamięć; wraca przy następnym napisie
BRAIN_IDLE_UNLOAD_SEC = 600.0
# pamięć podręczna buforów MLX. Domyślny limit MLX to ~1,5× zalecanej pamięci GPU
# i rośnie aż tam (na tym Macu ~55 GB) — zwolnione tensory ze STT i modelu decyzji zostają w RAM.
BRAIN_MLX_CACHE_BYTES = 256 * 1024 * 1024
# sufit aktywnej pamięci MLX: wagi Qwen 4-bit (~3 GB) + Parakeet + aktywacje, z zapasem
MLX_MEMORY_LIMIT_BYTES = 12 * 1024 * 1024 * 1024

# Tłumaczenie slangu modelem językowym (Gemma 3 12B, 4 bit, MLX) zamiast dosłownego Argos. Włączane flagą
# LIVEDUB_LLM_TRANSLATE=1 (model ~7,5 GB nie jest w paczce, tylko Mac z ≥ 32 GB). Argos zostaje zapasem:
# gdy model się ładuje, nie zdąży w LLM_TRANSLATE_TIMEOUT s albo odda śmieci, kwestia idzie przez Argos.
LLM_TRANSLATE_MODEL = "mlx-community/gemma-3-12b-it-4bit"
LLM_TRANSLATE_MIN_RAM_GB = 32
LLM_TRANSLATE_TIMEOUT = 1.6
LLM_TRANSLATE_EXTRA_BYTES = 10 * 1024 * 1024 * 1024
LLM_TRANSLATE_SYSTEM = (
    "Jesteś tłumaczem napisów do polskiego dubbingu gier i filmów kryminalnych (styl GTA). "
    "Tłumacz z angielskiego na naturalną, potoczną polszczyznę, jak mówi się na ulicy: angielski slang, przekleństwa i idiomy "
    "zamieniaj na polskie ODPOWIEDNIKI (nie dosłownie). Zachowaj osobę i sens (I → ja, you → ty), krótkie zdania nadające się "
    "do czytania na głos, bez wyjaśnień. Zwróć wyłącznie tłumaczenie."
)
LLM_TRANSLATE_SHOTS = (
    ("I'm gon roll me a fat one.", "Skręcę sobie grubego."),
    ("What's up, dawg? You good?", "No siema, stary. Wszystko git?"),
    ("That's sketchy as hell, I'm out.", "Śmierdzi na kilometr, spadam."),
    ("He's a snitch. Everybody knows.", "To kapuś. Wszyscy wiedzą."),
    ("We're so screwed.", "Jesteśmy w czarnej dupie."),
    ("Let's roll. Get in.", "Jedziemy. Wsiadaj."),
)


def llm_translate_wanted(env=None):
    """Czy ten proces ma tłumaczyć modelem (flaga + Mac z Apple Silicon, mlx-lm i dość pamięci)."""
    env = os.environ if env is None else env
    if not env.get("LIVEDUB_LLM_TRANSLATE"):
        return False
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    return importlib.util.find_spec("mlx_lm") is not None and _ram_gb() >= LLM_TRANSLATE_MIN_RAM_GB


DIALOGUE_QUESTION = (
    "To tekst odczytany przez OCR z ekranu gry albo filmu. Czy to kwestia dialogowa wypowiedziana przez postać?",
    ["tak — zdanie mówione przez postać", "nie — menu, podpowiedź przycisku, logo, znak wodny, napisy końcowe albo zlepek liter"],
)
INFO_QUESTION = (
    "Czy z tej kwestii widz dowie się czegoś nowego (fakt, plan, pytanie, polecenie, kto co zrobił)?",
    ["nie — to tylko okrzyk, reakcja albo potwierdzenie", "tak — jest w niej informacja"],
)
# mowa rozpoznana z dźwięku gry (bez napisu): rozmowa postaci czy radio w grze — piosenki, DJ, reklamy
# dostały p ≥ 0,89, dialogi z GTA p ≤ 0,02
RADIO_QUESTION = (
    "Tekst rozpoznany z dźwięku gry. Czy to rozmowa postaci w grze, czy piosenka albo audycja radiowa (DJ, reklama, wiadomości)?",
    ["piosenka albo radio", "rozmowa postaci"],
)
# wypowiedź NPC bez napisu: ktoś mówi do gracza albo o akcji (policja przez radio, ostrzeżenie, groźba,
# pytanie) — p 0,85–0,98, czy przypadkowe gadanie przechodniów — p 0,02–0,03
IMPORTANT_QUESTION = (
    "Wypowiedź postaci z gry (bez napisów). Czy ktoś mówi do gracza albo o akcji (ostrzeżenie, pytanie, polecenie, groźba, komunikat policji), czy to przypadkowe gadanie przechodniów w tle?",
    ["przypadkowe gadanie w tle", "mówi do gracza albo o akcji"],
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


def cap_mlx_cache():
    """Utnij podręczną pamięć MLX. Bez tego proces lektora zostaje przy dziesiątkach GB."""
    try:
        import mlx.core as mx

        mx.set_memory_limit(MLX_MEMORY_LIMIT_BYTES + (LLM_TRANSLATE_EXTRA_BYTES if llm_translate_wanted() else 0))
        mx.set_cache_limit(BRAIN_MLX_CACHE_BYTES)
        mx.clear_cache()
    except Exception:
        pass


class LektorBrain:
    """Pytania do modelu w tle; wyniki w pamięci podręcznej: (rodzaj, tekst) → rozkłady odpowiedzi.

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
    def check(self, text, kind="subtitle"):
        """Zleć ocenę (od razu wraca): kind="subtitle" — napis, "heard" — mowa z dźwięku. Wynik: verdict()."""
        if not text or self.failed:
            return
        key = (kind, text)
        with self._cond:
            if key in self._results or key in self._pending:
                return
            self._pending.add(key)
            # napis z ekranu przed mową z dźwięku: lektor czeka na jego ocenę tuż przed czytaniem, a
            # gadanie przechodniów na ulicy potrafi zapchać kolejkę
            if kind == "subtitle":
                self._jobs.appendleft((time.monotonic(), key))
            else:
                self._jobs.append((time.monotonic(), key))
            while len(self._jobs) > BRAIN_QUEUE_MAX:
                _t, old = self._jobs.pop() if self._jobs[-1][1][0] != "subtitle" else self._jobs.popleft()
                self._pending.discard(old)
            self._cond.notify_all()

    def verdict(self, text, wait=0.0, kind="subtitle"):
        """Napis: {"dialog": p, "info": p | None}; mowa: {"radio": p}. None, gdy modelu nie ma albo wynik
        nie zdążył w `wait` s."""
        key = (kind, text)
        deadline = time.monotonic() + max(0.0, wait)
        with self._cond:
            while True:
                if key in self._results:
                    return self._results[key]
                left = deadline - time.monotonic()
                if left <= 0 or key not in self._pending or not self.ready:
                    return None
                self._cond.wait(left)

    # --- wątek modelu --------------------------------------------------------------------
    def _run(self):
        cap_mlx_cache()
        try:
            import mlx.core as mx

            mx.set_default_device(mx.gpu)
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
                queued_at, key = self._jobs.popleft()
            kind, text = key
            if time.monotonic() - queued_at > BRAIN_JOB_TTL:
                with self._cond:
                    self._pending.discard(key)
                    self._cond.notify_all()
                continue
            if self.model is None and not self._load_logged():
                return
            result = None
            try:
                t0 = time.monotonic()
                if kind == "heard":
                    result = {"radio": self._ask(text, *RADIO_QUESTION)[0], "important": None}
                    if result["radio"] < 0.5:
                        result["important"] = self._ask(text, *IMPORTANT_QUESTION)[1]
                else:
                    result = {"dialog": self._ask(text, *DIALOGUE_QUESTION)[0], "info": None}
                    if result["dialog"] >= INFO_MIN_DIALOGUE:
                        result["info"] = self._ask(text, *INFO_QUESTION)[1]
                ms = (time.monotonic() - t0) * 1000
                if ms > 400:
                    self._log(f"model decyzji wolny: {ms:.0f} ms")
            except Exception as exc:
                self._log(f"model decyzji: błąd {exc}")
            with self._cond:
                self._pending.discard(key)
                if result is not None:
                    self._results[key] = result
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
        with HEAVY_IMPORT_LOCK:
            self._load_unlocked()

    def _load_unlocked(self):
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
        probs = [float(p) for p in mx.softmax(picked).tolist()]
        del ids, logits, picked
        mx.clear_cache()
        return probs


class LlmTranslator:
    """Tłumacz EN→PL: model językowy w stałym wątku (MLX), a gdy nie jest gotowy / nie zdąży / odda śmieci — Argos.

    translate() zawsze wraca w najgorszym razie po `timeout` s i zawsze oddaje tekst."""

    def __init__(self, fallback, timeout=LLM_TRANSLATE_TIMEOUT, log=None):
        self.fallback = fallback
        self.timeout = timeout
        self._log = log or (lambda _line: None)
        self.ready = False
        self.failed = None
        self.model = None
        self.tok = None
        self._jobs = deque()
        self._cond = threading.Condition()
        self._thread = None
        self._cache = OrderedDict()
        self._context = deque(maxlen=3)  # (angielski, polski) trzech ostatnich kwestii — ciągłość rozmowy
        self._context_lock = threading.Lock()

    # --- API zgodne z ArgosTranslator -----------------------------------------------------
    def ensure(self):
        self.fallback.ensure()
        self.start()

    def start(self):
        with self._cond:
            if self._thread is None and not self.failed:
                self._thread = threading.Thread(target=self._load_then_work, name="mlx-translate", daemon=True)
                self._thread.start()

    def translate(self, text):
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        out = self._ask(text) if self.ready else None
        if out is None:
            return self.fallback.translate(text)
        self._cache[text] = out
        while len(self._cache) > 256:
            self._cache.popitem(last=False)
        with self._context_lock:
            self._context.append((text, out))
        return out

    # --- wątek modelu ---------------------------------------------------------------------
    def _ask(self, text):
        job = {"text": text, "event": threading.Event(), "out": None, "dropped": False}
        with self._cond:
            self._jobs.append(job)
            self._cond.notify_all()
        if not job["event"].wait(self.timeout):
            job["dropped"] = True  # nikt już nie czeka na tę odpowiedź
            return None
        return job["out"]

    def _start_worker(self):
        with self._cond:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="mlx-translate", daemon=True)
                self._thread.start()

    def _load_then_work(self):
        cap_mlx_cache()
        try:
            import mlx.core as mx

            mx.set_default_device(mx.gpu)
        except Exception:
            pass
        try:
            t0 = time.monotonic()
            self._load()
            self.ready = True
            self._log(f"tłumacz slangu gotowy ({LLM_TRANSLATE_MODEL.split('/')[-1]}, {time.monotonic() - t0:.1f}s)")
        except Exception as exc:
            self.failed = str(exc)
            self._log(f"tłumacz slangu niedostępny: {exc}")
            return
        self._work()

    def _load(self):
        with HEAVY_IMPORT_LOCK:
            import mlx.core as mx
            from huggingface_hub import snapshot_download
            from mlx_lm import load

            path = snapshot_download(LLM_TRANSLATE_MODEL, local_files_only=True)
            model, tok = load(path)
            mx.eval(model.parameters())
            self.model, self.tok = model, tok
            self._complete(self._messages("Hello."))  # rozgrzewka: pierwszy przebieg kompiluje jądra

    def _work(self):
        while True:
            with self._cond:
                while not self._jobs:
                    self._cond.wait()
                job = self._jobs.popleft()
            if job["dropped"]:
                continue  # kwestia już poszła przez Argos
            try:
                job["out"] = self._clean(job["text"], self._complete(self._messages(job["text"])))
            except Exception as exc:
                self._log(f"tłumacz slangu: błąd {exc}")
                job["out"] = None
            job["event"].set()

    def _messages(self, text):
        msgs = [{"role": "system", "content": LLM_TRANSLATE_SYSTEM}]
        for src, dst in LLM_TRANSLATE_SHOTS:
            msgs += [{"role": "user", "content": src}, {"role": "assistant", "content": dst}]
        with self._context_lock:
            recent = list(self._context)
        for src, dst in recent:
            msgs += [{"role": "user", "content": src}, {"role": "assistant", "content": dst}]
        msgs.append({"role": "user", "content": text})
        return msgs

    def _complete(self, messages):
        from mlx_lm import generate

        try:
            prompt = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        return generate(self.model, self.tok, prompt=prompt, max_tokens=90, verbose=False)

    @staticmethod
    def _clean(src, raw):
        """Odpowiedź modelu → jedna linia po polsku albo None (wtedy Argos)."""
        text = " ".join((raw or "").split())
        if not text or len(text) > max(60, len(src) * 3) or "\n" in (raw or "").strip():
            return None
        if sum(ch.isalpha() for ch in text) < 2:
            return None
        if text.lower().strip(" .!?") == src.lower().strip(" .!?"):
            return None  # model oddał angielski bez zmian
        return text
