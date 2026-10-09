import copy
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
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from PIL import Image

try:
    from lektor_brain import HEAVY_IMPORT_LOCK, LektorBrain, brain_supported, cap_mlx_cache
except Exception:  # starsza paczka bez modułu — lektor działa na samych regułach
    LektorBrain = None
    HEAVY_IMPORT_LOCK = threading.RLock()

    def brain_supported():
        return False

    def cap_mlx_cache():
        return None

IS_WIN = sys.platform == "win32"
if IS_WIN:
    import gamereader_win as winplat

    winplat.enable_dpi_awareness()
# Windows: silnik działa bez konsoli — bez tej flagi każde ffmpeg/odtwarzacz otwierałby czarne okno
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}


POLL_MS = 80
MIN_INTERVAL = 0.10
DEFAULT_INTERVAL = 0.14
OCR_CONFIRM_FRAMES = 2
# Pierwsza klatka długiego napisu w grze bywa najgorsza („spontanfcznego…”).
# Czekamy chwilę i bierzemy najlepszy odczyt, zanim lektor go zatwierdzi.
OCR_SETTLE_SEC = 0.65
# W grze druga klatka (~150 ms) jest zwykle czystsza niż pierwsza. Tyle czekamy zawsze.
OCR_HOLD_SEC = 0.40


def extends_utterance(prev, nxt):
    """Nowy napis to rozwinięcie poprzedniego (OCR dogląda reszty zdania)."""
    left = polish_fold(prev or "")
    right = polish_fold(nxt or "")
    if not left or not right or len(right) <= len(left) + 1:
        return False
    # „Tata?” w „Nie nazywaj mnie tata, …” to nie rozwinięcie — środek pasuje tylko do dłuższego napisu
    return right.startswith(left) or (len(left) >= 12 and left in right and len(right) >= int(len(left) * 1.2))
BLACK_MEAN = 8.0
PREVIEW_W = 780
PREVIEW_H = 110
if IS_WIN:
    CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "LiveDub/config.json"
    CACHE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "LiveDub/Cache"
else:
    CONFIG_PATH = Path.home() / "Library/Application Support/GameReader/config.json"
    CACHE_DIR = Path.home() / "Library/Caches/GameReader"
# Lektor: Supertonic 3 (kod MIT, model OpenRAIL-M) — czysta polska wymowa.
# Synteza na CPU — na Macu szybsza od CoreML (zmierzone).
SUPERTONIC_VOICES = ("M5", "M2", "M3", "M4", "M1")
DEFAULT_SUPERTONIC_VOICE = "M5"

# Mastering: suchy, bliski mikrofon — ciepło, czytelność, kompresja, równa głośność.
# Poziom jest wyrównany już przy syntezie (RMS ~ -20 dBFS); głośność/emocja idzie PRZED limiterem,
# więc głośniejsze kwestie nie przesterowują.
LEKTOR_FILTER = (
    "highpass=f=70,"
    "equalizer=f=160:t=q:w=1.0:g=2,"
    "equalizer=f=3200:t=q:w=1.2:g=3,"
    "acompressor=threshold=-22dB:ratio=3:attack=5:release=90:makeup=2"
)
# VoiceStudio przychodzi już czysty — tylko lekkie wyrównanie, bez kompresji Supertonic
LEKTOR_CLOUD_FILTER = "highpass=f=60,acompressor=threshold=-18dB:ratio=2:attack=10:release=150:makeup=1"
LEKTOR_LIMITER = "alimiter=limit=0.89:attack=4:release=60:level=disabled"
LEKTOR_TARGET_RMS = 0.1

def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def text_key(text):
    return hashlib.sha1(normalize_text(text).lower().encode("utf-8")).hexdigest()


def utterance_tail(prev, nxt):
    """Końcówka `nxt`, której nie ma w `prev` (napis dopisał resztę zdania)."""
    left = polish_fold(prev or "")
    words = normalize_text(nxt or "").split()
    if not left or not words:
        return ""
    idx = polish_fold(" ".join(words)).find(left)
    if idx < 0:
        return ""
    covered = idx + len(left)
    acc = 0
    for i, word in enumerate(words):
        acc += len(polish_fold(word))
        if acc >= covered:
            return normalize_text(" ".join(words[i + 1 :]))
    return ""


# OCR łapie napis w trakcie pojawiania się/znikania: kilka słów przekręconych naraz i śmieć z HUD na
# brzegu („Parie Kenneth, omawjalismy fihansobanle?”, „Colt Panuje nad sytuacja, Zgamij…”) — to ta
# sama kwestia. Inna kwestia wymienia całe słowo („do warsztatu”/„do garażu”: 0,53; „mój”/„twój”: 0,57),
# a słowo przekręcone przez OCR zostaje podobne (≥ 0,62).
OCR_TWIN_MIN_FOLD = 15
OCR_TWIN_RATIO = 0.80
OCR_TWIN_WORD_RATIO = 0.60
OCR_TWIN_EDGE_JUNK = 5


def _fold_words(text):
    return [w for w in (polish_fold(t) for t in (text or "").split()) if w]


def _same_words_any_order(wa, wb):
    """Dwulinijkowy napis, którego linijki OCR złożył w innej kolejności („Suką! Jimmy nazwał mnie” /
    „Jimmry nazwał mnie sukaln”): te same słowa (z literówkami), inny porządek."""
    if min(len(wa), len(wb)) < 3 or len(wa) != len(wb):
        return False
    rest = list(wb)
    hits = 0
    for word in wa:
        best = max(rest, key=lambda w: SequenceMatcher(None, word, w, autojunk=False).ratio(), default=None)
        if best is not None and SequenceMatcher(None, word, best, autojunk=False).ratio() >= OCR_TWIN_WORD_RATIO:
            rest.remove(best)
            hits += 1
    return hits == len(wa)


def ocr_twins(a, b):
    """Ten sam napis przeczytany przez OCR dwa razy z literówkami w kilku słowach (nie inna kwestia)."""
    wa, wb = _fold_words(a), _fold_words(b)
    # Krótkie „To tutaj.” nie osiąga progu 15 znaków. Vision myli końcowe
    # j z i/l („To tutai.”, „To tutal.”), co wcześniej uruchamiało głos ponownie.
    if wa and len(wa) == len(wb) and all(
        left == right or (
            len(left) >= 5 and len(left) == len(right)
            and left[:-1] == right[:-1] and {left[-1], right[-1]} <= {"i", "j", "l"}
        )
        for left, right in zip(wa, wb)
    ):
        return True
    if _same_words_any_order(wa, wb):
        return True
    ja, jb = "".join(wa), "".join(wb)
    if min(len(ja), len(jb)) < OCR_TWIN_MIN_FOLD or min(len(ja), len(jb)) / max(len(ja), len(jb)) < 0.75:
        return False
    if SequenceMatcher(None, ja, jb, autojunk=False).ratio() < OCR_TWIN_RATIO:
        return False
    # Kilka błędów tej samej klatki („Jeev bezdomny: Nie ma dokąd pójść” /
    # „Jest bezdomny. Ne ma dekad posr”). Długi identyczny wyraz kotwiczy
    # porównanie; nie wystarcza podobna długość dwóch różnych zdań.
    if len(wa) == len(wb) and len(wa) >= 5:
        anchors = [x for x, y in zip(wa, wb) if x == y]
        if len(anchors) >= 2 and any(len(w) >= 7 for w in anchors):
            def compatible(x, y):
                if x == y:
                    return True
                known_x = x in _POLISH_BY_FOLD or x in _POLISH_FOLDED
                known_y = y in _POLISH_BY_FOLD or y in _POLISH_FOLDED
                if known_x and known_y:
                    return False  # dwa różne poprawne słowa to nie literówka
                return SequenceMatcher(None, x, y, autojunk=False).ratio() >= 0.4
            if all(compatible(x, y) for x, y in zip(wa, wb)):
                return True
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        left, right = "".join(wa[i1:i2]), "".join(wb[j1:j2])
        if tag == "replace":
            if SequenceMatcher(None, left, right, autojunk=False).ratio() < OCR_TWIN_WORD_RATIO:
                return False
            continue
        # dopisane/zgubione słowo: śmieć z HUD na brzegu albo krótkie słówko w środku
        edge = (i1 == 0 and j1 == 0) or (i2 == len(wa) and j2 == len(wb))
        if len(left) + len(right) > (OCR_TWIN_EDGE_JUNK if edge else 2):
            return False
    return True


_MIXED_CASE = re.compile(r"[a-ząćęłńóśźż][A-ZĄĆĘŁŃÓŚŹŻ]")


def _tail_like(words, tail):
    """Urywek końca kwestii przeczytany przez OCR z literówkami („dornu” = „domu”, „dom” = ucięte „domu”)."""
    for got, want in zip(words, tail):
        if got == want or (len(got) >= 3 and want.startswith(got)):
            continue
        if len(got) < 3 or SequenceMatcher(None, got, want, autojunk=False).ratio() < 0.6:
            return False
    return True


def folds_match(fold_a, fold_b):
    """Ten sam napis po złożeniu (polish_fold), mimo migania OCR: brak końcówki albo 1–3 literówki
    („więc”/„wiçc”, „żeń-szeń”/„żeń-szeńi”). Wspólne dla kolejki i dla „już przeczytane”."""
    if not fold_a or not fold_b:
        return False
    if fold_a == fold_b:
        return True
    short, long = (fold_a, fold_b) if len(fold_a) <= len(fold_b) else (fold_b, fold_a)
    # Hogwarts OCR miga — traktuj prawie-to-samo jako to samo
    if len(short) >= 8 and short in long and len(short) / len(long) >= 0.72:
        return True
    if abs(len(fold_a) - len(fold_b)) <= 4 and min(len(fold_a), len(fold_b)) >= 8:
        if _lev(fold_a, fold_b) <= 3:
            return True
    return False


def same_utterance(a, b):
    left = normalize_text(a or "").lower()
    right = normalize_text(b or "").lower()
    if not left or not right:
        return False
    return left == right or folds_match(polish_fold(left), polish_fold(right)) or ocr_twins(left, right)


def is_black_frame(frame):
    return frame is None or frame.size == 0 or float(np.mean(frame)) < BLACK_MEAN


PL_MARK = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
EN_HINTS = {
    "the", "you", "and", "that", "this", "what", "have", "don't", "it's", "we",
    "i", "to", "of", "is", "are", "was", "were", "not", "your", "my", "me",
}


PL_FOLD = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)

POLISH_UI = (
    "przeglądaj", "ukryj", "menu", "wróć", "wybierz", "wybór", "wybieraj",
    "ustawienia", "opcje", "zapisz", "wczytaj", "kontynuuj", "nowa", "gra",
    "wyjście", "wyjdz", "wyjdź", "pauza", "start", "zagraj", "dalej", "wstecz",
    "tak", "nie", "akceptuj", "odrzuć", "anuluj", "potwierdź", "gotowe",
    "dźwięk", "grafika", "sterowanie", "klawiatura", "mysz", "pad",
    "ekwipunek", "mapa", "zadania", "dziennik", "umiejętności", "broń", "bronie",
    "plecak", "postać", "poziom", "doświadczenie", "inventarz", "ekwipunek",
    "sieć", "zaproszenie", "multiplayer", "tryb", "trudność", "łatwy", "trudny",
    "średni", "język", "polski", "napisy", "głośność", "muzyka", "efekty",
    "pełny", "ekran", "okno", "rozdzielczość", "jakość", "niska", "wysoka",
    "średnia", "zastosuj", "domyślne", "wczytywanie", "zapis", "slot",
    "kontynuacja", "nowa gra", "wczytaj grę", "zakończ", "koniec", "powrót",
    "do", "wyboru", "wybor", "ukryj menu", "pokaż", "pokaż menu",
)

# Częste słowa dialogowe z polskimi znakami — OCR Vision często gubi diakrytyki.
POLISH_DIALOGUE = (
    "że", "się", "już", "też", "coś", "ktoś", "gdzieś", "kiedyś", "który", "która",
    "które", "którym", "których", "której", "którego", "czymś", "kimś", "czyimś",
    "więcej", "jeszcze", "może", "muszę", "chcę", "mogę", "wiem", "powiem",
    "iść", "pójść", "przyjść", "wrócić", "zrobić", "stało", "stała", "stał", "stałem", "stałam",
    "proszę", "przepraszam", "dziękuję", "dzięki", "witaj", "cześć", "dzień",
    "źle", "zły", "zła", "złe", "złego", "złej", "złym", "złymi", "źli",
    "dobry", "dobra", "dobre", "dobrego", "dobrej", "dobrym", "dobrymi", "dobrze",
    "bardzo", "naprawdę", "oczywiście",
    "właśnie", "wszystko", "wszyscy", "każdy", "każda", "żaden", "żadna",
    "nigdy", "zawsze", "teraz", "później", "wcześniej", "dzisiaj", "wczoraj",
    "jutro", "tutaj", "tam", "stąd", "dokąd", "skąd", "dlaczego", "dlatego",
    "ponieważ", "jednak", "zatem", "więc", "albo", "albo", "bądź", "albo",
    "przecież", "chyba", "pewnie", "raczej", "zupełnie", "całkiem", "trochę",
    "zupełnie", "absolutnie", "koniecznie", "natychmiast", "ostrożnie",
    "spokojnie", "szybko", "powoli", "ciszej", "głośniej", "uwaga", "pomoc",
    "pomóż", "pomocy", "ratunku", "uciekaj", "uciekajcie", "zostań", "chodź",
    "chodźcie", "idź", "idźcie", "wracaj", "wróć", "wróćcie", "patrz", "patrzcie",
    "słuchaj", "słuchajcie", "mów", "powiedz", "powiedzcie", "zapomnij",
    "pamiętaj", "pamiętajcie", "zrozumiałem", "zrozumiałam", "rozumiem",
    "niepokój", "niebezpieczeństwo", "bezpiecznie", "niebezpiecznie",
    "przyjaciel", "przyjaciele", "wróg", "wrogowie", "nauczyciel", "uczeń",
    "uczniowie", "profesor", "pani", "pan", "książę", "księżniczka",
    "czarodziej", "czarodzieje", "czarownica", "magia", "magiczny", "magiczne",
    "zaklęcie", "zaklęcia", "różdżka", "miotła", "eliksir", "eliksiry",
    "Hogwart", "Hogwartu", "Hogwarcie", "Gryffindor", "Slytherin",
    "Hufflepuff", "Ravenclaw", "dementor", "dementorzy", "bazyliszek",
    "Łódź", "łódź", "łodzi", "łódzki",
    "Łódź", "łódź", "łodzi", "łódzki",
    "smok", "smoki", "feniks", "hipogryf", "troll", "goblin", "centaur",
    "czarna", "magia", "śmierć", "śmierci", "życie", "życia", "dusza", "dusze",
    "serce", "krew", "wojna", "walka", "bitwa", "zwycięstwo", "porażka",
    "prawda", "kłamstwo", "tajemnica", "sekret", "przepowiednia", "los",
    "przeznaczenie", "odwaga", "strach", "lęk", "nadzieja", "miłość",
    "nienawiść", "złość", "radość", "smutek", "ból", "rana", "leczenie",
    "szkoła", "lekcja", "zajęcia", "biblioteka", "zamek", "wieża", "loch",
    "lochy", "las", "jezioro", "most", "brama", "pokój", "korytarz",
    "schodami", "schodów", "piętro", "piwnicy", "dach", "niebo", "księżyc",
    "słońce", "gwiazda", "gwiazdy", "noc", "nocy", "dzień", "dnia",
    "godzina", "chwila", "moment", "czas", "czasu", "roku", "lat",
    "młody", "młoda", "stary", "stara", "wielki", "wielka", "mały", "mała",
    "silny", "silna", "słaby", "słaba", "mądry", "mądra", "głupi", "głupia",
    "ważne", "ważny", "ważna", "pilne", "pilny", "trudne", "łatwe",
    "niemożliwe", "możliwe", "konieczne", "wystarczy", "wystarczyło",
    "musisz", "musicie", "powinieneś", "powinnaś", "powinniście",
    "chcesz", "chcecie", "możesz", "możecie", "wiesz", "wiecie",
    "widzę", "widzisz", "widzicie", "słyszę", "słyszysz", "czuję",
    "myślę", "myślisz", "sądzę", "uważam", "wydaje", "wydaje mi się",
    "boję", "boisz", "martwię", "martwisz", "cieszę", "cieszy",
    "kocham", "kochasz", "nienawidzę", "nienawidzisz", "przepraszam",
    "wybacz", "wybaczcie", "dziękuję", "proszę", "błagam", "błagam cię",
    "natychmiast", "natychmiastowo", "ostatecznie", "wreszcie", "w końcu",
    "nareszcie", "tymczasem", "potem", "najpierw", "następnie", "wreszcie",
    "skądinąd", "ponadto", "jednocześnie", "tym bardziej", "co najmniej",
    "przynajmniej", "najwyżej", "najbardziej", "najmniej", "coraz",
    "zupełnie", "całkowicie", "całkiem", "prawie", "niemal", "ledwie",
    "zaledwie", "dopiero", "jeszcze nie", "już nie", "nigdy więcej",
    "na zawsze", "na razie", "do zobaczenia", "do widzenia", "powodzenia",
    "uważaj", "uważajcie", "ostrożnie", "spokojnie", "cicho", "szybciej",
    "wolniej", "bliżej", "dalej", "wyżej", "niżej", "wewnątrz", "na zewnątrz",
    "naprzód", "wstecz", "w lewo", "w prawo", "prosto", "do przodu",
    "stój", "stójcie", "czekaj", "czekajcie", "zaczekaj", "zaczekajcie",
    "chodźmy", "idźmy", "wracajmy", "spróbuj", "spróbujcie", "spróbujmy",
    "zrób", "zróbcie", "zróbmy", "weź", "weźcie", "daj", "dajcie",
    "zabierz", "zostaw", "zostawcie", "otwórz", "zamknij", "schowaj",
    "pokaż", "pokażcie", "znajdź", "znajdźcie", "szukaj", "szukajcie",
    "zabij", "zabijcie", "ocal", "ocalcie", "uratuj", "uratujcie",
    "broń", "brońcie", "atakuj", "uciekać", "walczyć", "czarować",
    "rzucić", "zaklęcie", "użyć", "eliksiru", "różdżki", "miotły",
    "jestem", "jesteś", "jest", "jesteśmy", "jesteście", "są",
    "byłem", "byłam", "byłeś", "byłaś", "był", "była", "było", "byli",
    "będę", "będziesz", "będzie", "będziemy", "będziecie", "będą",
    "miałem", "miałam", "miałeś", "miałaś", "miał", "miała", "miało",
    "mam", "masz", "ma", "mamy", "macie", "mają", "mieć",
    "chciałem", "chciałam", "chciałbym", "chciałabym", "chciał", "chciała",
    "musiałem", "musiałam", "musiał", "musiała", "mogłem", "mogłam",
    "mogłem", "można", "trzeba", "warto", "należy", "powinien", "powinna",
    "powinni", "powinny", "niech", "oby", "żeby", "aby", "by",
    "gdyby", "jeśli", "jeżeli", "kiedy", "gdy", "podczas", "zanim",
    "dopóki", "aż", "nim", "skoro", "skoro", "choć", "chociaż", "mimo",
    "pomimo", "bez", "przez", "przez", "według", "wśród", "obok", "koło",
    "przy", "nad", "pod", "przed", "po", "za", "od", "do", "na", "w",
    "ze", "z", "ku", "dla", "o", "u", "poza", "wobec", "względem",
    "mną", "tobą", "nim", "nią", "nami", "wami", "nimi", "mnie", "ciebie",
    "jego", "jej", "nas", "was", "ich", "mi", "ci", "mu", "jej", "nam",
    "wam", "im", "mój", "moja", "moje", "twój", "twoja", "twoje",
    "swój", "swoja", "swoje", "nasz", "nasza", "nasze", "wasz", "wasza",
    "ich", "ten", "ta", "to", "ci", "te", "tamten", "tamta", "tamto",
    "ów", "owa", "owo", "taki", "taka", "takie", "jaki", "jaka", "jakie",
    "ile", "ilu", "kilka", "kilku", "wiele", "wielu", "mało", "dużo",
    "nic", "nikt", "nigdzie", "nigdy", "żaden", "żadna", "żadne",
    "każdy", "każda", "każde", "wszystek", "wszystka", "wszystko",
    "inny", "inna", "inne", "sam", "sama", "samo", "sami", "same",
    "pierwszy", "pierwsza", "drugi", "druga", "trzeci", "trzecia",
    "ostatni", "ostatnia", "następny", "następna", "poprzedni",
    "nowy", "nowa", "stary", "stara", "młody", "młoda", "dawny",
    "przyszły", "przyszła", "obecny", "obecna", "dawniej", "niedawno",
    "niedługo", "wkrótce", "zaraz", "od razu", "natychmiast", "późno",
    "wcześnie", "dawno", "niedawno", "czasami", "czasem", "często",
    "rzadko", "zawsze", "nigdy", "wiecznie", "na zawsze",
)

# Prefiksy przyimka „ze” — nie zamieniaj na „że”
_ZE_KEEP_NEXT = {
    "soba", "sobą", "mna", "mną", "mnie", "wszystkim", "wszystkimi", "wszystkich",
    "szkoly", "szkoły", "zamku", "lasu", "stolu", "stołu", "stolika", "siebie",
    "srodka", "środka", "strachu", "zlosci", "złości", "zalu", "żalu", "zazdrości",
    "smutku", "serca", "skaly", "skały", "sciany", "ściany", "sufitu", "schadow",
    "schodow", "schodów", "sypialni", "sali", "skrzydla", "skrzydła",
}


def polish_fold(text):
    return re.sub(r"[^a-z]", "", (text or "").translate(PL_FOLD).lower())


def _lev(a, b):
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _preserve_case(src, dst):
    if not src or not dst:
        return dst
    if src.isupper():
        return dst.upper()
    if src[0].isupper():
        return dst[:1].upper() + dst[1:]
    return dst


_POLISH_FOLDED = {polish_fold(word): word for word in POLISH_UI if polish_fold(word)}
_POLISH_BY_FOLD = {}
for _word in (*POLISH_UI, *POLISH_DIALOGUE):
    _w = (_word or "").strip()
    if not _w or " " in _w:
        continue
    _f = polish_fold(_w)
    if not _f:
        continue
    _POLISH_BY_FOLD.setdefault(_f, [])
    if _w not in _POLISH_BY_FOLD[_f]:
        _POLISH_BY_FOLD[_f].append(_w)


def _pick_polish_form(part, folded):
    """Wybierz formę z diakrytykami, jeśli OCR zgubił polskie znaki."""
    # krótkie homografy — osobna logika (ze/że)
    if folded in ("ze", "z", "a", "i", "o", "u", "w", "na", "po", "do", "od", "za", "by"):
        return None
    cands = _POLISH_BY_FOLD.get(folded) or []
    if not cands:
        return None
    if any(ch in PL_MARK for ch in part):
        # OCR już dał diakrytyk — nie psuj
        return None
    marked = [w for w in cands if any(ch in PL_MARK for ch in w)]
    plain = [w for w in cands if not any(ch in PL_MARK for ch in w)]
    if len(marked) == 1:
        return marked[0]
    if not marked and len(plain) == 1:
        return plain[0]
    if marked:
        return marked[0]
    return None


def _match_polish_word(folded, fuzzy=False):
    if not folded or re.fullmatch(r"[ijl]+", folded):
        return None
    if folded in _POLISH_FOLDED:
        return _POLISH_FOLDED[folded]
    picked = _pick_polish_form(folded, folded)
    if picked:
        return picked
    if len(folded) >= 4:
        for key, word in _POLISH_FOLDED.items():
            if len(key) >= 5 and key.startswith(folded) and len(key) - len(folded) <= 3:
                return word
    if not fuzzy or len(folded) < 6:
        return None
    best = None
    best_d = 99
    for key, words in _POLISH_BY_FOLD.items():
        if abs(len(key) - len(folded)) > 2:
            continue
        dist = _lev(folded, key)
        if dist < best_d:
            best_d = dist
            best = words[0]
    return best if best and best_d <= 2 else None


def _fix_ze_conjunction(text):
    """OCR często czyta «że» jako «ze» — popraw, gdy to spójnik."""
    parts = re.split(r"(\s+)", text)
    out = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        if polish_fold(tok) == "ze":
            nxt = ""
            j = i + 1
            while j < len(parts) and not parts[j].strip():
                j += 1
            if j < len(parts):
                nxt = parts[j]
            nxt_fold = polish_fold(nxt)
            keep = (
                nxt_fold in {polish_fold(x) for x in _ZE_KEEP_NEXT}
                or (nxt_fold[:1] in "szśźż" and nxt_fold not in ("sie", "sa", "sam", "sama", "samo", "sami"))
            )
            if not keep:
                out.append(_preserve_case(tok, "że"))
                i += 1
                continue
        out.append(tok)
        i += 1
    return "".join(out)


# cudzysłowy i apostrofy — lektor ich nie czyta, a OCR myli „ ” z ' ' ' i przecinkami
_QUOTES = "\"'`´‘’‚‛“”„‟«»‹›″′"
_QUOTE_CLASS = "[" + re.escape(_QUOTES) + "]"
_LETTER = r"[^\W\d_]"
# litery spoza polskiego alfabetu, które Vision dokleja zamiast cudzysłowu („okazjachiằ”)
_OCR_FOREIGN = re.compile(r"[\u0300-\u036f\u1e00-\u1eff\u0100-\u0103\u0108-\u010f\u0112-\u0117\u011a-\u0131\u0134-\u013e\u0147\u0148\u014c-\u0151\u0154-\u0159\u015c-\u015f\u0162-\u0178]")


def ocr_junk_count(text):
    """Ile śmieci OCR w tekście: obce litery i ciągi cudzysłowów („' ' '”) — do wyboru lepszego odczytu."""
    raw = text or ""
    foreign = sum(1 for ch in raw if ch.isalpha() and ch not in PL_MARK and ord(ch) > 127)
    return foreign + len(_OCR_FOREIGN.findall(raw)) + 2 * len(re.findall(rf"{_QUOTE_CLASS}(?:\s*{_QUOTE_CLASS})+", raw))


def ocr_reading_rank(text):
    """Wyższy wynik = odczyt bliższy prawdziwemu polskiemu napisowi (słownik, bez åøé)."""
    raw = normalize_text(text)
    if not raw:
        return -1
    table = _pl_diacritics()
    score = 0
    for word in re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+", raw):
        fold = polish_fold(word)
        if fold in table or fold in _POLISH_BY_FOLD:
            score += 12
        # bez premii za ogonki: „moją łódź znika”, „Fłanklin” wygrywały z poprawnymi — o ogonkach
        # decyduje dopiero remis, po tym, który wariant OCR czytał najczęściej
    score -= 14 * ocr_junk_count(raw)
    # „Dobrał Idź”, „Kurwal Jebany” — OCR zrobił z „!” literę ł/l; wariant z „!” jest lepszy
    known = lambda fold: fold in table or fold in _POLISH_BY_FOLD
    for word, tail in re.findall(r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]{3,})([łl])\.?(?=\s+[A-ZĄĆĘŁŃÓŚŹŻ])", raw):
        if known(polish_fold(word)) or not known(polish_fold(word + tail)):
            score -= 4
    return score


def ocr_reading_unsettled(text):
    """Długa klatka z obcymi literami albo zlepkami („spontanfcznego…”) — czekaj dłużej niż jedną klatkę."""
    raw = normalize_text(text)
    if ocr_junk_count(raw):
        return True
    words = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+", raw)
    # „O kurwa!”, „A ty?”, „W porządku.” — jednoliterowe polskie słowa to nie zlepek OCR
    if any(len(word) == 1 and word.lower() not in "aiouwz" for word in words):
        return True
    table = _pl_diacritics()
    long = [polish_fold(word) for word in words if len(word) >= 6]
    if len(long) < 3:
        return False
    unknown = sum(1 for fold in long if fold not in table and fold not in _POLISH_BY_FOLD)
    return unknown * 2 > len(long)


def ocr_same_flicker(a, b):
    """Dwa rozmazane odczyty tej samej kwestii, których same_utterance jeszcze nie łączy."""
    if same_utterance(a, b):
        return True
    if not (ocr_reading_unsettled(a) and ocr_reading_unsettled(b)):
        return False
    left, right = polish_fold(a), polish_fold(b)
    if min(len(left), len(right)) < 12:
        return False
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.72


def strip_ocr_quotes(text):
    """Usuwa cudzysłowy (zostaje apostrof w środku słowa) i obce litery doklejone przez OCR."""
    raw = _OCR_FOREIGN.sub("", text or "")
    raw = re.sub(rf"(?<!{_LETTER}){_QUOTE_CLASS}+|{_QUOTE_CLASS}+(?!{_LETTER})", " ", raw)
    raw = re.sub(r"\s+([!?…,.;:])", r"\1", raw)
    raw = re.sub(r"^[\s,.;:]+", "", raw)
    # „ka ,Proszę” → „ka, Proszę”; liczby („1,5”) zostają bez zmian
    raw = re.sub(rf"([,!?…;:])(?={_LETTER})", r"\1 ", raw)
    raw = re.sub(rf"(?<={_LETTER}{{2}})\.(?=[A-ZĄĆĘŁŃÓŚŹŻ])", ". ", raw)
    return normalize_text(raw)


def _normalize_polish_punct(text):
    raw = text or ""
    raw = raw.replace("…", "…").replace("...", "…").replace("..", "…")
    raw = raw.replace("！", "!").replace("？", "?")
    raw = strip_ocr_quotes(raw)
    raw = re.sub(r"\s+([!?…,.;:])", r"\1", raw)
    raw = re.sub(r"([!?…]){2,}", lambda m: m.group(0)[0] if m.group(0)[0] in "…" else m.group(0)[:2], raw)
    return normalize_text(raw)


_PL_DIACRITICS = None


def _pl_diacritics():
    """Słownik ~20 tys. częstych słów z napisów: „bledy” → „błędy” (pl_diacritics.tsv obok silnika)."""
    global _PL_DIACRITICS
    if _PL_DIACRITICS is None:
        table = {}
        try:
            with open(Path(__file__).resolve().with_name("pl_diacritics.tsv"), encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("#"):
                        continue
                    fold, _tab, word = line.rstrip("\n").partition("\t")
                    if word:
                        table[fold] = word
        except OSError:
            pass
        _PL_DIACRITICS = table
    return _PL_DIACRITICS


def _restore_from_dict(token):
    """Uzupełnij zgubione ogonki („blędy”, „bledy” → „błędy”), jeśli reszta liter się zgadza."""
    word = _pl_diacritics().get(polish_fold(token))
    if not word or len(word) != len(token) or word == token.lower():
        return None
    for have, want in zip(token.lower(), word):
        if have != want and (have in PL_MARK or have != polish_fold(want)):
            return None  # OCR dał inny ogonek niż w słowniku — nie zgadujemy
    return _preserve_case(token, word)


def repair_polish_ocr(text):
    """Poprawia błędy OCR PL: śmieci, zgubione diakrytyki, «ze»→«że»."""
    raw = _normalize_polish_punct(text)
    if not raw:
        return ""
    dialogue = bool(re.search(r"[!?…]", raw)) or len(raw) >= 18
    # słownik ogonków tylko dla polskiego tekstu — angielskie „zone” nie może zostać „żonę”
    polish_text = not should_translate(raw)
    if polish_text:
        raw = raw.replace("ç", "ę").replace("Ç", "Ę")  # OCR myli „ę” z „ç” („wiçc”)
    parts = re.split(r"([0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)", raw)
    out = []
    for part in parts:
        if not part:
            continue
        if not re.fullmatch(r"[0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+", part):
            out.append(part)
            continue
        if re.fullmatch(r"[0-9IlJ1S|]+", part) and len(part) <= 2:
            continue
        cleaned = re.sub(
            r"(?<=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])[0-9]+|[0-9]+(?=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])",
            "",
            part,
        )
        token = cleaned or part
        folded = polish_fold(token)
        big = _restore_from_dict(token) if polish_text and len(folded) >= 3 else None
        if big:
            out.append(big)
            continue
        restored = _pick_polish_form(token, folded)
        if restored:
            out.append(_preserve_case(token, restored))
            continue
        if dialogue:
            out.append(token)
            continue
        word = _match_polish_word(folded, fuzzy=bool(re.search(r"[0-9]", part)))
        if word:
            out.append(_preserve_case(token, word))
        elif folded and not re.fullmatch(r"[0-9IlJ1S|]+", part):
            out.append(re.sub(r"[0-9]", "", token) or token)
    fixed = _fix_ze_conjunction(normalize_text("".join(out)))
    return normalize_text(fixed)


# częste angielskie słowa — przeważają nad polskimi = tekst angielski
EN_COMMON = EN_HINTS | {
    "in", "on", "it", "do", "go", "be", "he", "she", "they", "with", "for", "get", "can", "just",
    "know", "here", "there", "now", "come", "going", "want", "got", "all", "right", "okay", "yeah",
    "no", "yes", "let's", "gonna", "him", "her", "us", "them", "out", "up", "if", "so", "but",
}
# słowa wspólne dla obu języków („to”, „i”, „we”…) nie rozstrzygają
EN_PL_SHARED = {"to", "i", "a", "o", "we", "no", "na", "do", "on", "go", "ta", "tak"}


def looks_polish(text):
    raw = normalize_text(text)
    if not raw:
        return False
    if any(ch in PL_MARK for ch in raw):
        return True
    words = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż']+", raw.lower())
    en_hits = sum(1 for word in words if word in EN_COMMON and word not in EN_PL_SHARED)
    hits = 0
    pl_only = 0
    for word in words:
        if "'" in word:
            continue
        token = polish_fold(word)
        if token in _POLISH_BY_FOLD or token in _POLISH_FOLDED or any(
            key.startswith(token) or token.startswith(key) for key in _POLISH_BY_FOLD if len(token) >= 4 and len(key) >= 4
        ):
            hits += 1
            pl_only += word not in EN_PL_SHARED
    # „to”, „i”, „we” są w obu językach — same nie przeważą nad angielskimi słowami
    return hits >= 1 and pl_only >= en_hits


def should_translate(text):
    raw = normalize_text(text)
    if not raw or looks_polish(raw):
        return False
    tokens = set(re.findall(r"[a-z']+", raw.lower()))
    return len(tokens & (EN_COMMON - EN_PL_SHARED)) >= 1 or (len(tokens) >= 4 and not looks_polish(raw))


def usable_ocr(text):
    raw = normalize_text(text)
    if len(raw) < 2:
        return False
    if re.fullmatch(r"[\d\s:%./+\-–—]+", raw):
        return False
    return sum(ch.isalpha() for ch in raw) >= 2


# Opisy dla niesłyszących w napisach (Netflix, YouTube): „[hiszpański]”, „[śmiech]”, „[Music]” —
# lektor ich nie czyta. Także ucięte przez OCR na początku: „[hiszpański Co…”, „hiszpański] Szlag!”.
_SUB_TAG = re.compile(r"\[[^\[\]]{1,40}\]")
# opis małymi literami („[śmiech]”) to nigdy przycisk („[E]”, „[Spacja]”) — można go wyciąć przed filtrem menu
_SUB_TAG_LOWER = re.compile(r"\[[a-ząćęłńóśźż][a-ząćęłńóśźż ,.\-]{2,39}\]")
_SUB_TAG_HEAD = re.compile(
    r"^\s*(?:\[[a-ząćęłńóśźż ]{3,30}\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])|[a-ząćęłńóśźż ]{3,30}\]\s*)"
)


def strip_subtitle_tags(text, lowercase_only=False):
    raw = _SUB_TAG_HEAD.sub("", (_SUB_TAG_LOWER if lowercase_only else _SUB_TAG).sub(" ", text or ""))
    return normalize_text(re.sub(r"\s+([,.!?…])", r"\1", raw))


def trim_ocr_edges(text):
    """Śmieci OCR na brzegach napisu: kropka listy, myślnik dialogowy, „statku. -”, „alejkę. r”."""
    raw = normalize_text(text)
    raw = re.sub(r"^[¡¿•·∙◦|/\\*]+\s*", "", raw)
    raw = re.sub(r"^[-–—]+\s+", "", raw)
    raw = re.sub(r"(?:\s+[¡¿•·∙◦|/\\*\-–—,;]+)+$", "", raw)
    raw = re.sub(r"([.!?…])\s+[A-Za-z•·]$", r"\1", raw)
    # HUD za napisem: „…rodzicielskie. ołzz”, „…wykończy! Wiad”, „…brzmi. p r ]” — napis kończy się
    # interpunkcją, więc 1–3 krótkie słowa bez niej po końcu zdania to nie dialog
    def hud_tail(match):
        words = [polish_fold(w) for w in match.group(0).split()]
        known = [w for w in words if len(w) >= 3 and (w in _pl_diacritics() or w in _POLISH_BY_FOLD)]
        return match.group(0) if known else ""  # „Czas odbić łódź” bez kropki to nadal zdanie

    return re.sub(r"(?<=[.!?…])(?:\s+[^\s.!?…,]{1,6}){1,3}(?:\s+[\]\[.|])?$", hud_tail, raw)


# Tekst z ekranu, który nie jest dialogiem: powiadomienia z pulpitu (GitHub, terminal), ścieżki,
# menu gry pisane wielkimi literami, podpowiedzi przycisków. „junk” = pomiń od razu,
# „suspect” = pomiń, jeśli model decyzji (lektor_brain) też uzna, że to nie dialog.
_UI_HARD = re.compile(
    r"(?i)claude/|pull request|\bcommit\b|build livedub|\bminutes?\b|https?://|www\.|\bdlss\b|[<>{}\\|]|#\s*\d|#:"
)
_UI_SOFT = re.compile(
    r"(?i)\b(?:tryb fotograficzny|ustawienia|naciśnij|przytrzymaj|wciśnij|anuluj|potwierdź|wczytywanie|zapisywanie|"
    r"kontynuuj|menu|przy pomocy)\b|(?:^|\s)[LRXB△○□✕](?=[\s.,)]|$)|•"
)


# tablica rejestracyjna auta w kadrze (GTA: „San Andreas” + kod „85BNR757”/„BNR 757”) — to nie dialog
_PLATE_CODE = re.compile(r"(?<![\w])(?:\d{0,2}[A-Z]{2,4}\s?\d{3,4}|\d{1,2}[A-Z]{3}\d{2,4})(?![\w])")


def looks_like_plate(text):
    raw = normalize_text(text)
    if not raw or len(raw.split()) > 6:
        return False
    if _PLATE_CODE.search(raw):
        return True
    if looks_polish(raw):
        return False
    return _plate_context(raw)


def _plate_word(word):
    fold = polish_fold(word)
    if not fold:
        return True  # sam znak interpunkcji / śmieć OCR
    if _PLATE_CODE.fullmatch(word.strip(".,:;!?")) or re.fullmatch(r"[A-ZĄĆĘŁŃÓŚŹŻËÑ]{2,4}[^\w\s]*\w{0,3}", word):
        return True
    if fold in _SAN_LIKE or re.fullmatch(r"\d{2,4}", fold or word):
        return True
    return len(fold) >= 5 and SequenceMatcher(None, fold, "andreas", autojunk=False).ratio() >= 0.75


_SAN_LIKE = ("san", "sen", "sea", "saa", "sn", "sas")


def _andreas_like(word):
    fold = polish_fold(word)
    return len(fold) >= 5 and SequenceMatcher(None, fold, "andreas", autojunk=False).ratio() >= 0.75


def _plate_context(raw):
    """Obok „Andreas” jest coś z tablicy: „San”, kod albo skrót WIELKIMI literami — samo „Andrés!” to imię."""
    if _PLATE_CODE.search(raw):
        return True
    words = raw.split()
    for i, word in enumerate(words):
        after_san = i > 0 and polish_fold(words[i - 1]) in _SAN_LIKE
        if after_san and len(polish_fold(word)) >= 4 and SequenceMatcher(
            None, polish_fold(word), "andreas", autojunk=False
        ).ratio() >= 0.5:
            return True  # „San Andirce EIG”: po „San” wystarczy luźne podobieństwo
        if not _andreas_like(word):
            continue
        near = words[max(0, i - 1) : i] + words[i + 1 : i + 2]
        if any(polish_fold(w) in ("san", "sen", "sea", "saa", "sn") or re.fullmatch(r"[A-ZËÑ]{3,4}\W*", w) for w in near):
            return True
    return False


_CAPS = "A-ZĄĆĘŁŃÓŚŹŻ"
_JUNK_TOKEN = re.compile(
    rf"^(?:[{_CAPS}][{_CAPS}.\-]+|[A-Za-ząćęłńóśźż{_CAPS}]*[a-ząćęłńóśźż][{_CAPS}][A-Za-ząćęłńóśźż{_CAPS}]*"
    rf"|[{_CAPS}]{{2,}}[a-ząćęłńóśźż]+)[^\w\s]*$"  # „TRici•” — dwie wielkie, potem małe
)
# skróty, które padają w dialogach GTA — nie są śmieciem z HUD
_REAL_CAPS = {"ok", "fbi", "lspd", "noose", "iaa", "cia", "usa", "gta", "nie", "tak", "hej", "stoj", "dea", "lsd", "tv"}


def strip_lead_junk(text):
    """Śmieć z HUD przed napisem: 1–2 słowa WIELKIMI literami (też z „.”/„-”) albo z wielką literą w środku,
    a za nimi zwykłe zdanie („PANCE Wątpię, bo…”, „PE BICE Może…”, „tANG Więc…”) — zostaje samo zdanie."""
    raw = normalize_text(text)
    words = raw.split()
    cut = 0
    while cut < min(2, len(words) - 1) and _JUNK_TOKEN.match(words[cut]) and polish_fold(words[cut]) not in _REAL_CAPS:
        cut += 1
    if not cut or len(polish_fold("".join(words[:cut]))) < 3:  # „pĘL Odpalamy…” też
        return raw
    if not re.match(rf"[-–„\"]?[{_CAPS}][a-ząćęłńóśźż]", words[cut]):
        return raw
    out = " ".join(words[cut:])
    return out if len(polish_fold(out)) >= 4 else raw


def strip_known_prefix(text, known):
    """1–3 słowa śmieci z kadru przed znaną już kwestią („Ubermach Tam! Tam!…”, „są Andrese KRYSTEL
    Jeśli…”) — zostaje sama kwestia. None, gdy tekst nie jest znaną kwestią z doklejonym przodem."""
    words = normalize_text(text).split()
    want = polish_fold(known or "")
    if len(want) < 8:
        return None
    whole = SequenceMatcher(None, polish_fold(text), want, autojunk=False).ratio()
    for cut in range(1, min(3, len(words) - 1) + 1):
        if re.search(r"[.!?…,:;]$", words[cut - 1]):
            break  # „Dasz radę. Jak będzie…” — przód to zdanie kwestii, nie śmieć z kadru
        tail = " ".join(words[cut:])
        if not re.match(rf"[-–„\"]?[{_CAPS}]", tail):
            continue
        got = polish_fold(tail)
        ratio = SequenceMatcher(None, got, want, autojunk=False).ratio()
        if abs(len(got) - len(want)) <= max(3, len(want) // 8) and ratio >= 0.88 and ratio > whole + 0.04:
            return tail
    return None


def lone_hud_word(text):
    """Sam śmieć z HUD bez napisu: jedno słowo WIELKIMI literami bez interpunkcji („MARNE”, „PANCE”)."""
    raw = normalize_text(text)
    return bool(re.fullmatch(rf"[{_CAPS}][{_CAPS}.\-]{{3,8}}", raw)) and polish_fold(raw) not in _REAL_CAPS


def strip_plate(text):
    """Tablica rejestracyjna doklejona z brzegu do napisu („San Andreas BNR Dostań się do domu.”) —
    wycięta; zostaje sam napis (albo nic, gdy to była tylko tablica)."""
    raw = normalize_text(text)
    words = raw.split()
    if not _plate_context(raw):
        return raw
    start, end = 0, len(words)
    while start < end and (
        _plate_word(words[start])
        or (start > 0 and polish_fold(words[start - 1]) in _SAN_LIKE and not looks_polish(words[start]))
    ):
        start += 1
    while end > start and _plate_word(words[end - 1]):
        end -= 1
    return " ".join(words[start:end])


def screen_junk_level(text):
    raw = normalize_text(text)
    if _UI_HARD.search(raw):
        return "junk"
    # podpowiedzi przycisków: dwa znaki interfejsu naraz („UKRYJ MENU (, PRZEGLĄDAJ L R”) — pewny
    # śmieć także przed wczytaniem modelu decyzji
    if len(_UI_SOFT.findall(raw)) >= 2:
        return "junk"
    letters = [ch for ch in raw if ch.isalpha()]
    caps = sum(ch.isupper() for ch in letters) / max(1, len(letters))
    if len(letters) >= 12 and caps > 0.7:
        # gra z dialogami WIELKIMI LITERAMI nie może zamilknąć — polskie zdanie rozstrzyga model
        return "suspect" if looks_polish(raw) else "junk"
    if _UI_SOFT.search(raw) or (len(letters) >= 6 and caps > 0.7):
        return "suspect"
    return "ok"


class RecurringLead:
    """Nazwa strefy / etykieta z mapy doklejana PRZED różnymi kwestiami („Lotnisko n Tato!…”, „Lotnisko n
    Coś ci się…”, „Wiad To mnie…”). Słowo, które stało na początku 2 RÓŻNYCH kwestii bez interpunkcji po
    sobie, to nie dialog — od tej pory jest wycinane (także w wariantach OCR: „Lotniako”, „Miad”)."""

    def __init__(self, keep=40, store=None):
        self.recent = deque(maxlen=keep)
        self.junk = deque(maxlen=24)
        # wyuczone etykiety zostają między sesjami — „Lotnisko n …” nie trafia do lektora po restarcie
        self.store = store
        if store is not None:
            try:
                self.junk.extend(str(t) for t in json.loads(Path(store).read_text(encoding="utf-8"))[-24:])
            except (OSError, ValueError, TypeError):
                pass

    def _save(self):
        if self.store is None:
            return
        try:
            Path(self.store).parent.mkdir(parents=True, exist_ok=True)
            Path(self.store).write_text(json.dumps(list(self.junk), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _lead(words):
        """(pierwsze słowo przodu, reszta) albo None: 1–2 słowa bez interpunkcji, potem zdanie z wielkiej litery."""
        for cut in (1, 2):
            if len(words) < cut + 2:
                return None
            head = words[:cut]
            if any(re.search(r"[.,!?…:;]$", w) and len(polish_fold(w)) > 2 for w in head):
                return None
            if re.match(rf"[-–„\"]?[{_CAPS}][a-ząćęłńóśźż]", words[cut]):
                token = max(head, key=lambda w: len(polish_fold(w)))
                if len(polish_fold(token)) >= 3:
                    return polish_fold(token), polish_fold(" ".join(words[cut:]))
        return None

    def _is_junk(self, fold):
        return any(
            abs(len(fold) - len(j)) <= 2 and SequenceMatcher(None, fold, j, autojunk=False).ratio() >= 0.7
            for j in self.junk
        )

    def observe(self, text):
        lead = self._lead(normalize_text(text).split())
        if not lead:
            return
        token, rest = lead
        for other_token, other_rest in self.recent:
            if (other_token == token and SequenceMatcher(None, rest, other_rest, autojunk=False).ratio() < 0.6
                    and not self._is_junk(token)):
                self.junk.append(token)
                self._save()
                break
        self.recent.append(lead)

    def strip(self, text):
        words = normalize_text(text).split()
        if not self.junk:
            return normalize_text(text)
        folds = [polish_fold(w) for w in words]
        if folds and all(self._is_junk(f) or len(f) <= 2 for f in folds) and any(len(f) >= 3 for f in folds):
            return ""  # sama etykieta z mapy („Lotnisko.”)
        for cut in (3, 2, 1):
            if len(words) < cut + 1:
                continue
            head = [polish_fold(w) for w in words[:cut]]
            # przód = nauczone słowo + najwyżej krótkie śmieci obok („n”, „n!”, „z”, „u *”)
            if any(self._is_junk(h) for h in head if len(h) >= 3) and all(
                self._is_junk(h) or len(h) <= 2 for h in head
            ):
                # „Lotnisko jest blisko.” to zdanie; „Lotnisku u * więć…” — śmieć z krótkim ogonem
                if re.match(rf"[-–„\"]?[{_CAPS}]", words[cut]) or any(len(h) <= 2 for h in head):
                    return " ".join(words[cut:])
        return normalize_text(text)


class RecurringFragments:
    """Znak wodny albo stały napis (logo kanału) doklejany przez OCR do różnych kwestii.

    Końcówka z co najmniej 4 słów, która w niedawnych napisach stała przy 2 RÓŻNYCH kwestiach, to nie
    dialog — zostaje wycięta (a gdy to cały napis, nie ma czego czytać). Warianty odczytu tej samej
    kwestii („x Właśnie…”, „Właśnie…”) się nie liczą."""

    MIN_WORDS = 4

    def __init__(self, keep=40):
        self.recent = deque(maxlen=keep)

    @staticmethod
    def _find_run(seq, run):
        n = len(run)
        return next((i for i in range(len(seq) - n + 1) if seq[i : i + n] == run), -1)

    @staticmethod
    def _alike(a, b):
        return a in b or b in a or SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.5

    def strip(self, text):
        words = normalize_text(text).split()
        toks = [(i, polish_fold(w)) for i, w in enumerate(words)]
        toks = [(i, f) for i, f in toks if f]
        seq = [f for _i, f in toks]
        cut = None
        for n in range(len(seq), self.MIN_WORDS - 1, -1):
            run = seq[-n:]
            head = "".join(seq[:-n])
            others = []
            for prev in self.recent:
                j = self._find_run(prev, run)
                if j < 0:
                    continue
                # Ten sam dialog z innym śmieciem OCR przed nim nie jest dowodem
                # na znak wodny. Porównujemy całe napisy, nie same różniące się
                # początki: długie losowe prefiksy też mogą mieć ponad 12 znaków.
                if SequenceMatcher(None, seq, prev, autojunk=False).ratio() >= 0.75:
                    continue
                other = "".join(prev[:j] + prev[j + n :])
                # początek przekręcony przez OCR („Jebany pedał”/„ebar, perlat”) to ta sama kwestia
                # krótki początek (śmieć z HUD + jedno słowo: „tANG Więc”) nie świadczy o innej kwestii
                if len(other) >= 12 and not (head and self._alike(other, head)) and not any(
                    self._alike(other, seen) for seen in others
                ):
                    others.append(other)
            if len(others) >= 2:
                cut = toks[-n][0]
                break
        if seq and seq not in self.recent:
            self.recent.append(seq)
        if cut is None:
            return normalize_text(text)
        return normalize_text(" ".join(words[:cut])).rstrip(" ,;:-–—")


_INTERROGATIVES = ("co", "kto", "kim", "kogo", "komu", "gdzie", "kiedy", "dlaczego", "czemu", "jak", "czy", "ile", "który", "która", "które")


def is_interjection(text):
    """Krótkie wtrącenie („Tak jest.”, „Jadę!”, „Hej, stary.”) — w zrywie pierwsze do pominięcia.
    Krótkie pytanie z treścią („Gdzie jesteśmy?”) wtrąceniem nie jest."""
    words = re.findall(r"[\wÀ-ž']+", text or "")
    if not words:
        return True
    if len(words) == 2 and (text or "").rstrip().endswith("?") and words[0].lower() in _INTERROGATIVES:
        return False
    return len(words) <= 2


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if not IS_WIN:
        try:
            os.chmod(CONFIG_PATH, 0o600)  # ustawienia tylko dla właściciela
        except OSError:
            pass


SAMPLE_RATE = 16000
# Wypowiedź = od ciszy do ciszy; transkrybujemy ją raz, w całości (bez wersji roboczych).
SILENCE_SEC = 0.35
MIN_SPEECH_SEC = 0.35
MAX_SPEECH_SEC = 8.0
PREROLL_SEC = 0.25
# „Napisy + dźwięk”: z dźwięku tłumaczymy wypowiedź postaci, której gra nie podpisała (NPC, bohater
# w trakcie jazdy) — tylko gdy w trakcie wypowiedzi nie było napisu (z zapasem), głos był wyraźny
# (nie tło), okresowy jak głos (nie muzyka ani wybuch), trwał jak zdanie, a rozpoznany tekst to pełne zdanie.
AUDIO_SUBTITLE_MARGIN = 0.5
AUDIO_MIN_SPEECH_SEC = 0.8
# radio w grze: śpiew trzyma równe nuty (mowa: 0,00–0,31 wysokości „stałej”, śpiew 0,76–1,00),
# (dźwięk bez pauzy — muzyka w lokalu — idzie do rozpoznania; DJ i reklamy odsiewa model decyzji)
AUDIO_SUNG_MAX = 0.55
BRAIN_RADIO_P = 0.5
# kwestia NPC bez napisu tłumaczona, gdy ważna dla gracza (policja, ostrzeżenie), nie gadanie przechodniów
BRAIN_IMPORTANT_P = 0.5
# komunikat w pętli (głośnik w sklepie: „Surveillance cameras are posted”) i powtarzane odzywki NPC —
# zdanie z dźwięku słyszane już w tym oknie czasu nie jest czytane drugi raz
HEARD_REPEAT_SEC = 900.0
HEARD_REPEAT_MIN_FOLD = 10
# ciąg dalszy rozmowy, którą lektor już zaczął czytać z dźwięku — czytany do końca, nawet gdy model
# uzna samą odpowiedź za mało ważną (inaczej rozmowa urywa się w połowie)
HEARD_CONVO_SEC = 15.0
AUDIO_MIN_VOICED = 0.10  # mowa: 0,17 pod szumem 6 dB … 0,35 czysta; szum i wybuchy: 0,00
# kwestia czekająca w kolejce dłużej niż tyle sekund (a jest już nowsza) — przepada, lektor leci dalej
CATCH_UP_STALE_SEC = 2.0
# model decyzji (lektor_brain): poniżej tego p(dialog) napis to nie kwestia postaci; napis „podejrzany”
# wg reguł (menu, przyciski) odpada już przy wyższym progu. Kwestia bez informacji (p < próg) w zrywie
# wypada pierwsza — jak u lektora TV.
BRAIN_JUNK_P = 0.10
BRAIN_ODD_WAIT = 0.6
# cel misji i dłuższy napis wiszą na ekranie po 7–10 s, a OCR czyta je znów po mignięciu — dłuższa
# kwestia jest pamiętana dłużej niż krótkie „Nie.”/„Co?”, które w rozmowie padają kilka razy
SPOKEN_LONG_FOLD = 12
SPOKEN_LONG_HOLD_SEC = 20.0
# słowa napisów odrzuconych przez model (szyld, nazwa ulicy) — ich warianty z OCR odpadają od razu
JUNK_MEMORY_SEC = 90.0
BRAIN_SUSPECT_P = 0.35
BRAIN_SKIP_INFO_P = 0.2
# krótkie wtrącenie (do 2 słów, nie pytanie z treścią) wypada, dopóki model nie jest pewny informacji
BRAIN_SKIP_SHORT_P = 0.4
# Wejście jak lektor w filmie: gotowy głos rusza chwilę po tym, jak postać zaczyna mówić (dźwięk gry),
# a nie w chwili pojawienia się napisu. Czekamy najwyżej FILM_MAX_WAIT od napisu i nigdy, gdy lektor
# się spóźnia, czeka już następna kwestia albo postać przed chwilą skończyła mówić (to była ta kwestia).
FILM_DELAY = 0.3
FILM_MAX_WAIT = 0.9
FILM_MAX_LAG = 1.0
FILM_RECENT_VOICE = 0.8
# zanim silnik nauczy się tempa napisów w grze: typowy napis ~16 znaków na sekundę
SUBTITLE_CPS_DEFAULT = 16.0
# skracanie tekstu: od tylu sekund spóźnienia lektora za napisem (poziom 1 / poziom 2)
LAG_CONDENSE = 2.0
LAG_CONDENSE_STRONG = 3.5
# zdanie (3+ słowa), które lektor przed chwilą przeczytał, nie wraca w następnej kwestii — napis wjeżdża
# po kawałku, a OCR czyta nakładające się linie jako różne warianty
SENTENCE_REPEAT_SEC = 12.0
# ile kwestii może czekać w kolejce (więcej = najstarsza przepada)
SPEECH_QUEUE_MAX = 4
# polskie napisy + angielski dźwięk: przez tyle sekund od ostatniego polskiego napisu dźwięku
# nie rozpoznajemy ani nie tłumaczymy — służy tylko do emocji i ściszania gry
PL_SUBS_HOLD_SEC = 600.0
PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
SPEECH_RMS = 0.0025
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


def normalize_mode(mode):
    if mode in ("audio", "ocr"):
        return mode
    return "auto"


# Wspólne ustawienia odczytu (dostrojone na GTA VI) — RDR2 i inne gry działają tak samo.
# Pasek napisów w Chrome (Netflix/YouTube) ustawia osobno CHROME_BAND.
_GAME_BASE = {
    # napis zwykle pojawia się od razu w całości — drugi odczyt OCR tylko opóźnia start lektora
    "confirm_frames": 1,
    "interval": 0.15,
    "tts": 0.92,
    "boost": 2.4,
    "band": 0.18,
    "gap": 0.03,
    "inset": 0.10,
}

GAME_PROFILES = {
    "gta6": {
        **_GAME_BASE,
        "label": "GTA VI",
        "hint": "Na razie wycinki z Netflixa w Chrome (do testów); po premierze PS Remote Play.",
        # w trybie źródła „Auto” najpierw szukaj Netflixa w Chrome
        "prefer": "chrome",
    },
    "gta5": {
        **_GAME_BASE,
        "label": "GTA V",
        "hint": "PS Remote Play (PS5) — włącz w grze napisy; te same ustawienia co w każdej grze.",
    },
    "rdr2": {
        **_GAME_BASE,
        "label": "Red Dead Redemption 2",
        "hint": "Napisy na dole, szybkie kwestie — te same ustawienia co w każdej grze.",
    },
    "generic": {
        **_GAME_BASE,
        "label": "Inna gra",
        "hint": "Ustawienia jak w GTA VI. Jak nie łapie napisów — zaznacz pasek ręcznie.",
    },
}
# gry usunięte z listy: zapisany wybór przechodzi na najbliższy profil
_OLD_GAMES = {"gta": "gta6"}


def normalize_game(game):
    if game in GAME_PROFILES:
        return game
    if game in _OLD_GAMES:
        return _OLD_GAMES[game]
    return "generic" if game else "gta6"


def which_bin(name):
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        path = Path(folder) / name
        if path.is_file():
            return str(path)
    return None


def bundled_ffmpeg():
    """ffmpeg z pakietu imageio-ffmpeg (instalowany przy pierwszym uruchomieniu aplikacji)."""
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
        return path if path and Path(path).is_file() else None
    except Exception:
        return None


def _speakable(piece):
    piece = normalize_text(piece)
    if not piece or piece.lower() in JUNK_HEARD:
        return False
    return bool(re.search(r"[A-Za-zĄąĆćĘęŁłŃńÓóŚśŹźŻż]", piece))


# Odgłosy i wtrącenia, których lektor nie czyta (EN z dźwięku i PL z napisów).
_FILLER_RE = re.compile(
    r"^(?:h+m+|m+h*m+|mhm|u+h+|u+m+|a+h+|e+h+|e+r+m*|o+h+|u+g+h+|a+r+g+h+|o+o+f+|p+h+e+w+|huh|"
    r"(?:h+a+){2,}|(?:h+e+){2,}|h+e+h+|y{2,}|e{3,}|a{3,}|a+c+h+|o+c+h+|e+c+h+|u+f+|o+j+|u+u+|aha)$",
    re.IGNORECASE,
)
_WORD_EDGE = "\"'„”«»()[]*-–—…,.!?;:"


def _is_filler_word(word):
    core = word.strip(_WORD_EDGE).lower().replace("-", "")
    return bool(core) and bool(_FILLER_RE.match(core))


def strip_fillers(text):
    """Usuwa „hmm”, „uh”, „yyy”, „ach”… Zwraca "" gdy nie zostaje nic do przeczytania."""
    words = normalize_text(text or "").split()
    # sam śmiech („ha ha ha”) też pomijamy — ale „he” w zdaniu to angielskie „on”
    if words and all(_is_filler_word(w) or w.strip(_WORD_EDGE).lower() in ("ha", "he", "hah", "heh") for w in words):
        return ""
    kept = [w for w in words if not _is_filler_word(w)]
    if len(kept) != len(words):
        cleaned = normalize_text(" ".join(kept))
        cleaned = re.sub(r"^[,;:—–\-…. ]+", "", cleaned)
        cleaned = cleaned[:1].upper() + cleaned[1:] if cleaned else ""
    else:
        cleaned = normalize_text(text or "")
    letters = sum(ch.isalpha() for ch in cleaned)
    return cleaned if letters >= 3 else ""


# Delikatne emocje lektora, zdanie po zdaniu.
_LIVELY_WORDS = (
    "szybko", "uciekaj", "uważaj", "padnij", "stój", "rusz", "biegnij", "jedź", "gazu", "gliny",
    "pomocy", "cholera", "kurwa", "zabiję", "zabij", "strzelaj", "teraz", "natychmiast", "spadaj",
)
_SOFT_WORDS = (
    "przepraszam", "kocham", "tęsknię", "żegnaj", "cicho", "szepnij", "spokojnie", "przykro",
    "nie żyje", "umarł", "umarła", "pogrzeb",
)
# 8 kroków dyfuzji: mniej robotycznie niż 5 (sesja 27.09: 12 najlepsze, 8 = kompromis z czasem syntezy ~+55%)
SUPERTONIC_STEPS = 8
# tempo lektora (parametr speed Supertonic przy zwykłej kwestii; było 1,05)
LEKTOR_SPEED = 1.10
# najszybsze tempo samego modelu — powyżej Supertonic bełkocze (1,5 → 15 % słów źle rozpoznanych)
LEKTOR_MAX_SPEED = 1.35
# spóźniony lektor przyspiesza tylko tempem samego głosu. Bełkot z sesji 13:48 robiło rozciąganie
# ffmpeg (atempo ×1,12–1,40), nie tempo modelu — atempo wyłączone. Sesja 16:30: przy suficie ×1,15
# lektor stał na stałym tempie i nie nadążał za napisami (1–1,7 s za nimi) — sufit znów ×1,25.
LEKTOR_MAX_RATE = 1.25
LEKTOR_MAX_STRETCH = 1.0
# gdy w kolejce czeka już następny napis: kolejne fragmenty syntezują się szybciej, bez pauz
LEKTOR_CATCHUP_RATE = 1.08
# tempo dopasowane do napisów: lektor ma się zmieścić w czasie, w którym napis wisi na ekranie
LEKTOR_CPS_PRIOR = 15.0  # znaki/s lektora przy speed=1 — potem uczy się z własnych syntez
# nastrój z napisu (słowa i interpunkcja): tylko głośność i pauza — bez domieszki innego głosu
# i bez zmiany tempa, więc barwa lektora się nie zmienia
LEKTOR_MOOD_GAIN = {"calm": 1.0, "lively": 1.1, "soft": 0.9}


def lektor_punctuation(text, mood="calm"):
    """Interpunkcja napisu → (mnożnik głośności, pauza po fragmencie w s).

    „!” głośniej, „!!”/„?!” jeszcze głośniej, „…” ciszej i z dłuższą pauzą, „?” krótki oddech po pytaniu.
    Tekst dla lektora zostaje bez zmian — intonację pytania robi sam Supertonic."""
    t = normalize_text(text)
    gain = LEKTOR_MOOD_GAIN.get(mood, 1.0)
    if re.search(r"[!?]{2,}", t):
        gain = max(gain, 1.22)
    elif "!" in t:
        gain = max(gain, 1.14)
    end = t.rstrip("\"'”»)] ")
    pause = 0.0
    if end.endswith(("…", "...")):
        gain = min(gain, 0.9)
        pause = 0.15
    elif end.endswith("?"):
        pause = 0.06
    return gain, pause


def lektor_voice_params(arousal):
    """Przejęcie -1…1 → domieszka żywego głosu, tempo, głośność, pauza."""
    a = max(-1.0, min(1.0, float(arousal)))
    return {
        # domieszka drugiego głosu tylko lekka — przy mocnej lektor brzmiał jak inna osoba i gorzej
        # wymawiał; emocję niosą głównie tempo i głośność
        "blend": max(0.0, min(0.3, 0.05 + 0.3 * a)) if a > -0.15 else 0.0,
        "speed": 1.0 + (0.14 * a if a > 0 else 0.10 * a),
        "gain": 1.0 + (0.32 * a if a > 0 else 0.35 * a),
        "pause": max(0.05, 0.10 - 0.06 * a),
    }


def _sentence_mood(sentence):
    low = sentence.lower()
    if "!" in sentence or any(w in low for w in _LIVELY_WORDS):
        return "lively"
    if "…" in sentence or "..." in sentence or any(w in low for w in _SOFT_WORDS):
        return "soft"
    return "calm"


def lektor_segments(text):
    """Dzieli kwestię na zdania i skleja sąsiednie o tym samym nastroju: [(tekst, nastrój), …]."""
    pieces = [normalize_text(p) for p in re.findall(r"[^.!?…]+(?:[.!?…]+|$)", normalize_text(text or ""))]
    groups = []
    for piece in pieces:
        if not _speakable(piece):
            continue
        mood = _sentence_mood(piece)
        # krótkie zdanie („Jedź, jedź!”, „Hej!”) samo brzmi sztucznie — sklej z sąsiednim;
        # wygrywa mocniejszy nastrój (ożywiony > cichy > spokojny)
        # pierwszy fragment trzymaj krótki — lektor rusza dopiero, gdy jest zsyntezowany
        first_full = len(groups) == 1 and len(groups[0][0]) >= 25 and len(groups[0][0]) + len(piece) > 90
        if groups and not first_full and (groups[-1][1] == mood or len(piece) < 25 or len(groups[-1][0]) < 25):
            prev_text, prev_mood = groups[-1]
            rank = {"lively": 2, "soft": 1, "calm": 0}
            mood = max(mood, prev_mood, key=rank.get)
            groups[-1] = (f"{prev_text} {piece}", mood)
        else:
            groups.append((piece, mood))
    if not groups:
        return [(normalize_text(text), "calm")] if _speakable(text) else []
    # długi początek tnij na przecinku — lektor rusza szybciej, reszta syntezuje się w trakcie
    head, mood = groups[0]
    # (tylko bardzo długi: każdy podział to słyszalna przerwa między plikami audio)
    if len(head) > 110:
        cut = head.find(", ", 40)
        if 0 < cut < len(head) - 20:
            groups[0:1] = [(head[: cut + 1], mood), (head[cut + 2 :], mood)]
    return groups



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


def window_subtitle_band(win, profile):
    left, top, width, height = win
    frac = float(profile.get("band", 0.13))
    gap_frac = float(profile.get("gap", 0.035))
    inset_frac = float(profile.get("inset", 0.12))
    chrome = 0 if height >= 980 else 52
    body = max(140, height - chrome)
    band_h = max(52, int(body * frac))
    inset = max(24, int(width * inset_frac))
    gap = max(10, int(body * gap_frac))
    return (left + inset, top + height - band_h - gap, max(80, width - 2 * inset), band_h)


def _parse_win_line(text):
    parts = [piece.strip() for piece in str(text or "").replace("OK ", "", 1).split(",") if piece.strip()]
    if len(parts) != 4:
        return None
    try:
        win = tuple(int(float(piece)) for piece in parts)
    except ValueError:
        return None
    if win[2] < 160 or win[3] < 120:
        return None
    return win


def find_remote_play_window():
    info = _quartz_remote_play_info()
    if info is not None:
        return info[:4]
    return _helper_bin_window() or _helper_remote_play_window()


def _quartz_remote_play_info():
    """Zwraca (x, y, w, h, window_id) albo None."""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:
        return None
    pids = _remote_play_pids()
    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []
    best = None
    best_area = 0
    for win in windows:
        owner = str(win.get("kCGWindowOwnerName") or "").lower()
        title = str(win.get("kCGWindowName") or "").lower()
        layer = int(win.get("kCGWindowLayer") or 0)
        pid = int(win.get("kCGWindowOwnerPID") or 0)
        if layer != 0 or "gamereader" in owner:
            continue
        blob = f"{owner} {title}"
        by_name = any(token in blob for token in ("remote play", "remoteplay", "ps remote")) or (
            "playstation" in owner and "remote" in blob
        )
        by_pid = pid in pids
        if not by_name and not by_pid:
            continue
        bounds = win.get("kCGWindowBounds") or {}
        width = float(bounds.get("Width") or 0)
        height = float(bounds.get("Height") or 0)
        if width < 200 or height < 140:
            continue
        area = width * height
        if area > best_area:
            best_area = area
            wid = int(win.get("kCGWindowNumber") or 0)
            if wid <= 0:
                continue
            best = (
                int(bounds.get("X") or 0),
                int(bounds.get("Y") or 0),
                int(width),
                int(height),
                wid,
            )
    return best


SOURCES = ("auto", "ps", "chrome")
CHROME_OWNERS = ("google chrome", "chrome", "google chrome canary", "chromium")
VIDEO_TITLES = ("netflix", "youtube", "twitch", "max", "prime video", "disney")
# Netflix/YouTube: napisy wyżej i większe niż w grach
CHROME_BAND = {"band": 0.2, "gap": 0.07, "inset": 0.1}


def _quartz_chrome_info(video_only=False, titles=VIDEO_TITLES):
    """Okno Chrome (x, y, w, h, window_id, tytuł): najpierw karta z wideo, potem największe."""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:
        return None
    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    best = None
    best_score = 0.0
    for win in CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []:
        owner = str(win.get("kCGWindowOwnerName") or "").lower()
        if owner not in CHROME_OWNERS or int(win.get("kCGWindowLayer") or 0) != 0:
            continue
        title = str(win.get("kCGWindowName") or "")
        video = any(key in title.lower() for key in titles)
        if video_only and not video:
            continue
        bounds = win.get("kCGWindowBounds") or {}
        width = float(bounds.get("Width") or 0)
        height = float(bounds.get("Height") or 0)
        wid = int(win.get("kCGWindowNumber") or 0)
        if width < 320 or height < 200 or wid <= 0:
            continue
        score = width * height * (4.0 if video else 1.0)
        if score > best_score:
            best_score = score
            best = (int(bounds.get("X") or 0), int(bounds.get("Y") or 0), int(width), int(height), wid, title)
    return best


def _win_source_window(source, prefer):
    """Windows: to samo co find_source_window, z listy okien systemu."""
    if source == "auto" and prefer == "chrome":
        info = winplat.find_browser(video_only=True, video_titles=VIDEO_TITLES)
        if info is not None:
            return (*info[:5], "chrome", info[5])
    if source in ("auto", "ps"):
        info = winplat.find_remote_play()
        if info is not None:
            return (*info[:5], "ps", "PS Remote Play")
    if source in ("auto", "chrome"):
        info = winplat.find_browser(video_only=(source == "auto"), video_titles=VIDEO_TITLES)
        if info is not None:
            return (*info[:5], "chrome", info[5])
    return None


def find_source_window(source="auto", prefer="ps"):
    """(x, y, w, h, window_id, rodzaj, tytuł) okna źródła albo None. rodzaj: "ps" | "chrome".
    prefer="chrome": w trybie auto najpierw Netflix/YouTube w Chrome (np. GTA VI na wycinkach)."""
    if IS_WIN:
        return _win_source_window(source, prefer)
    if source == "auto" and prefer == "chrome":
        info = _quartz_chrome_info(video_only=True)
        if info is not None:
            return (*info[:5], "chrome", info[5])
    if source in ("auto", "ps"):
        info = _quartz_remote_play_info()
        if info is not None:
            return (*info[:5], "ps", "PS Remote Play")
        if source == "ps":
            win = find_remote_play_window()
            return (*win, 0, "ps", "PS Remote Play") if win else None
    if source in ("auto", "chrome"):
        # w trybie auto Chrome liczy się tylko z Netflixem/YouTube — zwykłe przeglądanie nie odpala lektora
        info = _quartz_chrome_info(video_only=(source == "auto"))
        if info is not None:
            return (*info[:5], "chrome", info[5])
    return None


# Napisy interfejsu odtwarzaczy (Netflix/YouTube) — to nie dialog
PLAYER_UI_TEXT = (
    "wstrzymane", "oglądasz", "ogladasz", "pomiń czołówkę", "pomin czolowke", "pomiń podsumowanie",
    "następny odcinek", "nastepny odcinek", "obejrzyj napisy końcowe", "odtwórz ponownie",
    "paused", "you're watching", "skip intro", "skip recap", "next episode", "watch credits",
    "pomiń reklamę", "skip ad",
)
PLAYER_UI_EXACT = ("reklama", "ad", "reklama.", "ad.")


def is_player_ui_text(text):
    low = normalize_text(text or "").lower()
    if not low:
        return False
    if low in PLAYER_UI_EXACT or any(low.startswith(item) for item in PLAYER_UI_TEXT):
        return True
    # „12:34”, „-1:02:10” — licznik czasu
    return bool(re.fullmatch(r"-?\d{1,2}(:\d{2}){1,2}", low))


PLAYER_PAUSED = ("wstrzymane", "paused")


def player_paused(text):
    """Netflix przy pauzie pokazuje „Wstrzymane” — wtedy nic nie czytamy."""
    low = normalize_text(text or "").lower()
    return any(re.search(rf"(?:^|\s){word}(?:$|\s|[.!?])", low) for word in PLAYER_PAUSED)


def strip_player_ui(text):
    """Wycina z odczytu OCR doklejone napisy odtwarzacza (licznik czasu, „Pomiń czołówkę”…)."""
    out = normalize_text(text or "")
    for item in PLAYER_UI_TEXT:
        out = re.sub(re.escape(item), " ", out, flags=re.IGNORECASE)
    out = re.sub(r"(?<!\S)-?\d{1,2}(?::\d{2}){1,2}(?!\S)", " ", out)
    return normalize_text(out)


def player_controls_visible(info):
    """Netflix/YouTube pokazuje pasek sterowania (pauza, ruch myszą) — na dole jest wtedy tytuł, nie dialog.
    Poznajemy go po czerwonym pasku postępu w dolnej części okna."""
    if not info:
        return False
    x, y, w, h = info[:4]
    strip = capture_remote_play_band(x, y + int(h * 0.72), w, int(h * 0.28), info=info)
    if strip is None:
        return False
    b, g, r = strip[:, :, 0].astype(int), strip[:, :, 1].astype(int), strip[:, :, 2].astype(int)
    red = (r > 170) & (g < 70) & (b < 80)
    # pasek postępu = poziomy ciąg czerwieni (co najmniej kilkanaście pikseli w jednym wierszu)
    return bool(red.size) and int(red.sum(axis=1).max()) >= 14


def capture_remote_play_band(left, top, width, height, info=None):
    """Szybki zrzut paska z okna źródła (Quartz w procesie, bez spawn helpera)."""
    if IS_WIN:
        # PS5: obraz z samego okna PS Remote Play — to, co je zasłania (powiadomienia, inne okna), nie wchodzi
        if info is not None and len(info) > 5 and info[5] == "ps" and info[4]:
            frame = winplat.grab_window(info[4], left, top, width, height)
            if frame is not None:
                return frame
        # reszta (i zapas): zrzut ekranu; okna LiveDub są wyłączone z przechwytywania (setContentProtection)
        return winplat.grab(left, top, width, height)
    try:
        from Quartz import (
            CGWindowListCreateImage,
            CGRectNull,
            kCGWindowListOptionIncludingWindow,
            kCGWindowImageBoundsIgnoreFraming,
            kCGWindowImageNominalResolution,
        )
    except Exception:
        return None
    info = info or _quartz_remote_play_info()
    if info is None or not info[4]:
        return None
    wx, wy, ww, wh, wid = info[:5]
    try:
        full = CGWindowListCreateImage(
            CGRectNull,
            kCGWindowListOptionIncludingWindow,
            wid,
            kCGWindowImageBoundsIgnoreFraming | kCGWindowImageNominalResolution,
        )
    except Exception:
        return None
    if full is None:
        return None
    try:
        from Quartz import CGImageGetWidth, CGImageGetHeight, CGImageCreateWithImageInRect, CGRectMake
    except Exception:
        return None
    fw, fh = int(CGImageGetWidth(full)), int(CGImageGetHeight(full))
    if fw < 8 or fh < 8:
        return None
    scale_x = fw / max(ww, 1)
    scale_y = fh / max(wh, 1)
    rx = int(round((left - wx) * scale_x))
    ry = int(round((top - wy) * scale_y))
    rw = int(round(width * scale_x))
    rh = int(round(height * scale_y))
    rx = max(0, min(rx, fw - 1))
    ry = max(0, min(ry, fh - 1))
    rw = max(4, min(rw, fw - rx))
    rh = max(4, min(rh, fh - ry))
    try:
        piece = CGImageCreateWithImageInRect(full, CGRectMake(rx, ry, rw, rh))
    except Exception:
        return None
    if piece is None:
        return None
    try:
        from io import BytesIO

        from Cocoa import NSBitmapImageRep, NSBitmapImageFileTypePNG

        rep = NSBitmapImageRep.alloc().initWithCGImage_(piece)
        if rep is None:
            return None
        data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
        if data is None:
            return None
        image = Image.open(BytesIO(bytes(data))).convert("RGB")
        return np.ascontiguousarray(np.array(image)[:, :, ::-1])
    except Exception:
        return None


def _helper_bin_window():
    path = game_reader_bin()
    if path is None:
        return None
    try:
        result = subprocess.run(
            [str(path), "--win"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return _parse_win_line(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None


def _helper_remote_play_window():
    if not helper_available():
        return None
    try:
        line, sock, _rest = helper_call("WIN", timeout=3)
        sock.close()
        if line.startswith("OK"):
            return _parse_win_line(line)
    except OSError:
        return None
    return None


def _quartz_remote_play_window():
    info = _quartz_remote_play_info()
    return info[:4] if info is not None else None


def _remote_play_pids():
    pids = set()
    try:
        from AppKit import NSWorkspace

        for app in NSWorkspace.sharedWorkspace().runningApplications():
            bundle = str(app.bundleIdentifier() or "").lower()
            name = str(app.localizedName() or "").lower()
            if (
                "remoteplay" in bundle
                or "playstation" in bundle
                or "remote play" in name
                or "remoteplay" in name
            ):
                pids.add(int(app.processIdentifier()))
    except Exception:
        pass
    return pids


def subtitle_band_candidates(bounds):
    return [
        subtitle_band(bounds, 0.12, 0.03),
        subtitle_band(bounds, 0.16, 0.04),
        subtitle_band(bounds, 0.22, 0.02),
    ]


def game_reader_bin():
    env = os.environ.get("GAMEREADER_HELPER")
    here = Path(__file__).resolve()
    candidates = [
        Path(env) if env else None,
        here.parents[1] / "GameReaderHelper",
        here.parents[1] / "MacOS" / "GameReaderHelper",
        here.parents[1] / "MacOS" / "GameReader",
        Path.home() / "Applications/LiveDub.app/Contents/Resources/GameReaderHelper",
        Path.home() / "Applications/GameReader.app/Contents/Resources/GameReaderHelper",
        Path.home() / "Applications/GameReader.app/Contents/MacOS/GameReaderHelper",
        Path.home() / "Applications/GameReader.app/Contents/MacOS/GameReader",
        Path("/Users/kamil/gamer/macos/GameReader"),
    ]
    for path in candidates:
        if path is not None and path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def audio_tap_cmd(target="ps"):
    path = game_reader_bin()
    return [str(path), "--tap", "--tap-target", target] if path else None


def _hub_tcp():
    """Windows: Electron słucha na 127.0.0.1 — GAMEREADER_HUB = „host:port:token”."""
    raw = os.environ.get("GAMEREADER_HUB") or ""
    parts = raw.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1]), parts[2]


def helper_sock_path():
    env = os.environ.get("GAMEREADER_SOCK")
    if env:
        return Path(env)
    return Path.home() / "Library/Application Support/GameReader/helper.sock"


def helper_available():
    if IS_WIN:
        return _hub_tcp() is not None
    path = helper_sock_path()
    return path.exists() or path.is_socket()


def helper_call(cmd, timeout=20):
    hub = _hub_tcp() if IS_WIN else None
    if hub is not None:
        host, port, token = hub
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(f"{token} {cmd.rstrip()}\n".encode())
    else:
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


def screen_access_ok():
    """Czy macOS pozwala czytać ekran (silnik i helper). False = brak zgody „Nagrywanie ekranu”.

    Bez tej zgody nie widać tytułów okien ani napisów — silnik po cichu czytałby tylko z dźwięku."""
    if IS_WIN:
        return True  # Windows nie pyta o zgodę na zrzut ekranu
    try:
        from Quartz import CGPreflightScreenCaptureAccess

        if not CGPreflightScreenCaptureAccess():
            return False
    except Exception:
        pass
    return helper_has_screen() is not False


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
    def __init__(self, target="ps", duck=False):
        # "ps" = PS Remote Play, "chrome" = Google Chrome (Netflix, YouTube…)
        self.target = target
        # duck = przejmij dźwięk gry (Core Audio tap) i pozwól go ściszać, gdy mówi lektor
        self.duck = duck
        self.ducking = False
        self.proc = None
        self.sock = None
        self.chunks = queue.Queue()
        self.message = ""
        self.alive = False

    def start(self):
        if self.duck:
            try:
                self._start_proc([str(game_reader_bin()), "--duck", "--duck-target", self.target], stdin=True)
                self.ducking = True
                return
            except PermissionError:
                raise
            except Exception as exc:
                # nie wyszło (np. gra jeszcze nic nie gra) — zwykły nasłuch, bez ściszania
                self.duck_error = str(exc)
                self.stop()
        if helper_available():
            self._start_helper()
            return
        cmd = audio_tap_cmd(self.target)
        if cmd is None:
            raise RuntimeError("Brak pomocnika dźwięku (--tap). Przebuduj aplikację.")
        self._start_proc(cmd)

    def set_gain(self, value):
        """Głośność gry 0…1 (tylko w trybie ściszania)."""
        if not self.ducking or self.proc is None or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(f"GAIN {max(0.0, min(1.0, float(value))):.2f}\n".encode())
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _start_proc(self, cmd, stdin=False):
        if not cmd or cmd[0] in ("None", ""):
            raise RuntimeError("Brak pomocnika dźwięku. Przebuduj aplikację.")
        self.message = ""
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
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
        # błąd pomocnik wypisuje tuż przed wyjściem — daj mu chwilę, żeby nie uznać startu za udany
        if "LISTENING" not in self.message and "AUDIO_OK" not in self.message:
            time.sleep(0.3)
        if self.proc.poll() is not None:
            text = self.message or "Gra nie oddaje dźwięku."
            if any(word in text.lower() for word in ("tcc", "zgody", "przechwytywania", "denied", "not permitted")):
                raise PermissionError(text)
            raise RuntimeError(text)

    def _start_helper(self):
        line, sock, rest = helper_call(f"TAP {self.target}", timeout=16)
        if line.startswith("ERR"):
            sock.close()
            text = line[4:].strip() or "PS Remote Play nie oddaje dźwięku."
            if any(word in text.lower() for word in ("tcc", "zgody", "przechwytywania", "denied", "not permitted")):
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
        if self.proc and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()  # Ducker sprząta i oddaje dźwięk grze
            except OSError:
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def open_mic_settings():
    if IS_WIN:
        try:
            os.startfile("ms-settings:privacy-microphone")
        except OSError:
            pass
        return
    urls = [
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
        "x-apple.systempreferences:com.apple.Settings.PrivacySecurity.Privacy.Microphone",
    ]
    for url in urls:
        if subprocess.run(["open", url], capture_output=True).returncode == 0:
            return
    subprocess.run(["open", "/System/Applications/System Settings.app"], check=False)


def open_screen_settings():
    if IS_WIN:
        return
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
            info["CFBundleName"] = "LiveDub"
            info["CFBundleDisplayName"] = "LiveDub"
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


# Wymowa: „rz” czytane jako osobne r + z (zamarzać, marznąć…). Apostrof rozdziela
# głoski bez pauzy; „marzenie”, „marzę” (od marzyć) zostają z „ż”.
_PRONOUNCE_RULES = [
    (re.compile(r"(mar)(z(?:n|ł|l))", re.IGNORECASE), r"\1'\2"),
    (re.compile(r"\b((?:za|od|prze|przy|roz|do|u|ob|wy|z|prz)mar)(za)", re.IGNORECASE), r"\1'\2"),
    (re.compile(r"\b(tar)(zan)", re.IGNORECASE), r"\1'\2"),
    (re.compile(r"(mier)(zi|zł)", re.IGNORECASE), r"\1'\2"),
    # „Hej, stary.”, „Spoko, stary.” — Supertonic czyta po angielsku „stery”; podwójne „a” trzyma „stary”
    (re.compile(r"\b(sta)(ry)\b", re.IGNORECASE), lambda m: m.group(1) + m.group(1)[-1] + m.group(2)),
]


# Angielskie imiona i nazwy: polski lektor czyta je po polsku („Mi-cha-el”), więc podmieniamy
# pisownię na wymowę angielską zapisaną po polsku („Majkel”) — z polską odmianą
# (Jasona → Dżejsona, Lucię → Lusiję, Mike'a → Majka, Tony'ego → Toniego).
ENGLISH_NAMES = {
    # GTA VI / GTA V
    "Jason": "Dżejson", "Lucia": "Lus'ija", "Michael": "Majkel", "Trevor": "Trewor", "Lamar": "Lamar",
    "Lester": "Lester", "Amanda": "Amanda", "Tracey": "Trejsi", "Jimmy": "Dżimi", "Wade": "Łejd",
    "Floyd": "Flojd", "Ron": "Ron", "Devin": "Dewin", "Dave": "Dejw", "Steve": "Stiw", "Haines": "Hejns",
    "Norton": "Norton", "Townley": "Taunli", "Philips": "Filips", "Chop": "Czop", "Brad": "Bred",
    "Niko": "Niko", "Claude": "Klod", "Tommy": "Tomi", "Vercetti": "Wersetti", "Johnson": "Dżonson",
    "Ryder": "Rajder", "Sweet": "Słit", "Cal": "Kel", "Boobie": "Bubi", "Raul": "Raul",
    "Raymond": "Rejmond", "Ray": "Rej", "Nesto": "Nesto", "Joł": "Joł",
    # imiona
    "Mike": "Majk", "John": "Dżon", "Johnny": "Dżoni", "James": "Dżejms", "Jack": "Dżek", "Jake": "Dżejk",
    "Jim": "Dżim", "Joe": "Dżo", "Joey": "Dżoi", "Josh": "Dżosz", "Joshua": "Dżoszua", "Justin": "Dżastin",
    "Jessica": "Dżesika", "Jennifer": "Dżenifer", "Jenny": "Dżeni", "Jordan": "Dżordan", "Jay": "Dżej",
    "Jayden": "Dżejden", "Jesse": "Dżesi", "Jess": "Dżes", "Joel": "Dżoel", "Jackson": "Dżekson",
    "George": "Dżordż", "Charles": "Czarls", "Charlie": "Czarli", "Chase": "Czejs", "Chad": "Czed",
    "Chuck": "Czak", "Chris": "Kris", "Christopher": "Kristofer", "Kate": "Kejt", "Katie": "Kejti",
    "Steven": "Stiwen", "Ryan": "Rajan", "Brian": "Brajan", "Bryan": "Brajan", "Kyle": "Kajl",
    "Luke": "Luk", "Matthew": "Matju", "Matt": "Met", "Nathan": "Nejtan", "Nate": "Nejt", "Ethan": "Itan",
    "Aiden": "Ejden", "Hailey": "Hejli", "Heather": "Heder", "Timothy": "Timoti",
    "Stephanie": "Stefani", "Sean": "Szon", "Shawn": "Szon", "Shane": "Szejn", "Wayne": "Łejn",
    "Dwayne": "Dłejn", "William": "Łiljam", "Will": "Łil", "Willy": "Łili", "Walter": "Łolter",
    "White": "Łajt", "Rachel": "Rejczel", "Michelle": "Miszel", "Nicole": "Nikol", "Sarah": "Sera",
    "Emily": "Emili", "Olivia": "Oliwija", "Sophie": "Sofi", "Chloe": "Kloi", "Zoe": "Zoi", "Leah": "Lija",
    "Riley": "Rajli", "Taylor": "Tejlor", "Brandon": "Brendon", "Tyler": "Tajler", "Dylan": "Dilan",
    "Kevin": "Kewin", "Kenny": "Keni", "Keith": "Kit", "Mason": "Mejson", "Harry": "Hari", "Henry": "Henri",
    "Eddie": "Edi", "Freddy": "Fredi", "Bobby": "Bobi", "Billy": "Bili", "Danny": "Deni", "Ricky": "Riki",
    "Randy": "Rendi", "Andy": "Endi", "Ray": "Rej", "Roy": "Roj", "Troy": "Troj", "Earl": "Erl",
    "Cole": "Kol", "Casey": "Kejsi", "Cassie": "Kesi", "Stacy": "Stejsi", "Tracy": "Trejsi", "Blake": "Blejk",
    "Bruce": "Brus", "Grace": "Grejs", "Tony": "Toni", "Mia": "Mija", "Carl": "Karl", "Hugh": "Hju",
    "Grayson": "Grejson", "Nick": "Nik", "Frank": "Frenk", "Franklin": "Frenklin", 
    "Jenkins": "Dżenkins", "Smith": "Smit", "Jones": "Dżołns", "Brown": "Braun",
    # GTA VI (Leonida) — postaci i miejsca
    "Duval": "Duwal", "Caminos": "Kaminos", "Hampton": "Hempton", "Ike": "Ajk", "Dre'Quan": "Drikłan",
    "Drequan": "Drikłan", "Dimez": "Dajmz", "Roxy": "Roksi", "Heder": "Heder", "Bautista": "Bautista",
    "Bayside": "Bejsajd", "Starfish Island": "Starfisz Ajlend", "Ocean Beach": "Ołszen Bicz",
    "Little Haiti": "Litl Hejti", "Brickell": "Brikel", "Hialeah": "Hajalija", "Everglades": "Ewerglejds",
    # GTA V — postaci i miejsca
    "Los Santos": "Los Santos", "Blaine County": "Blejn Kaunti", "Sandy Shores": "Sendi Szors",
    "Paleto Bay": "Paleto Bej", "Mount Chiliad": "Maunt Czilijad", "Alamo Sea": "Alamo Si", "Grapeseed": "Grejpsid",
    "Del Perro": "Del Perro", "Rockford Hills": "Rokford Hils", "Mirror Park": "Miror Park", "Chumash": "Czumasz",
    "Davis": "Dejwis", "Humane Labs": "Hjumejn Labs", "Merryweather": "Meriłeder", "Lifeinvader": "Lajfinwejder",
    "FIB": "Ef Aj Bi", "IAA": "Aj Ej Ej", "Weston": "Łeston", "Simeon": "Simeon", "Yetarian": "Jetarian",
    "Madrazo": "Madraso", "Tanisha": "Tanisza", "Denise": "Deniz", "Stretch": "Strecz", "Solomon": "Solomon",
    "Richards": "Riczards", "Maude": "Mod", "Nigel": "Najdżel", "Barry": "Beri", "Cris": "Kris", "Formage": "Formaż",
    "Lazlow": "Lazlo", "Friedlander": "Fridlender", "Tonya": "Tonja", "Beverly": "Bewerli", "Epsilon": "Epsilon",
    "Cheng": "Czeng", "Wei": "Łej", "Tao": "Tał", "Hao": "Hał", "Mary-Ann": "Meri En", "Ammu-Nation": "Amju Nejszyn",
    "Leonida": "Leonida", "Gellhorn": "Gelhorn", "Ambrosia": "Ambrozja", "Grassrivers": "Grasriwers",
    "Kalaga": "Kalaga", "Leonida Keys": "Leonida Kiz", "Vice Beach": "Wajs Bicz", "Port Gellhorn": "Port Gelhorn",
    # Red Dead Redemption 2 — postaci
    "Arthur": "Artur", "Morgan": "Morgan", "Dutch": "Dacz", "Van der Linde": "Wan der Lynde", "Marston": "Marston",
    "Hosea": "Hołzeja", "Micah": "Majka", "Bell": "Bel", "Sadie": "Sejdi", "Adler": "Edler", "Abigail": "Abigejl",
    "Lenny": "Leni", "Kieran": "Kiran", "Tilly": "Tili", "Karen": "Karen", "Mary-Beth": "Meri Bet", "Molly": "Moli",
    "Susan": "Suzan", "Grimshaw": "Grimszo", "Pearson": "Pirson", "Strauss": "Sztraus", "Swanson": "Słonson",
    "Uncle": "Ankl", "Josiah": "Dżozaja", "Trelawny": "Trelołni", "Escuella": "Eskuela", "Williamson": "Łiljamson",
    "Colm": "Kolm", "O'Driscoll": "O'Driskol", "O'Driscolls": "O'Driskolów", "Cornwall": "Kornłol",
    "Leviticus": "Lewitikus", "Braithwaite": "Brejtłejt", "Milton": "Milton", "Pinkerton": "Pinkerton",
    "Pinkertons": "Pinkertonów", "Eagle Flies": "Igl Flajs", "Rains Fall": "Rejns Fol", "Hamish": "Hejmisz",
    "Downes": "Dałns", "Callander": "Kalander", "Mac": "Mek", "Davey": "Dejwi", "Reverend": "Rewerend",
    "Angelo Bronte": "Andżelo Bronte", "Bronte": "Bronte", "Guido": "Gwido", "Evelyn": "Ewelin",
    # Red Dead Redemption 2 — miejsca
    "Valentine": "Walentajn", "Saint Denis": "Sejnt Denis", "Blackwater": "Blekłoter", "Rhodes": "Rołds",
    "Strawberry": "Stroberi", "Annesburg": "Enzberg", "Van Horn": "Wan Horn", "Tumbleweed": "Tamblłid",
    "Armadillo": "Armadilo", "Emerald Ranch": "Emerald Rancz", "Horseshoe Overlook": "Horsszu Owerluk",
    "Clemens Point": "Klemens Point", "Shady Belle": "Szejdi Bel", "Beaver Hollow": "Biwer Holoł", "Colter": "Kolter",
    "Lemoyne": "Lemojn", "New Hanover": "Nju Hanower", "West Elizabeth": "Łest Elizabet", "New Austin": "Nju Ostin",
    "Bayou Nwa": "Baju Nła", "Heartlands": "Hartlendz", "Big Valley": "Big Weli", "Tall Trees": "Tol Triz",
    "Wapiti": "Łapiti", "Guarma": "Gwarma", "Flatneck": "Fletnek", "Twin Rocks": "Tłin Roks",
    # miejsca
    "Vice City": "Wajs Siti", "Liberty City": "Liberti Siti", "Miami": "Majami", "Grove Street": "Grołw Strit",
    "Vinewood": "Wajnłud", "Downtown": "Dałntaun", "Vespucci": "Wespuczi",
}
# hiszpański (Vice City / Leonida): imiona i wtrącenia z dialogów — wymowa hiszpańska po polsku
SPANISH_WORDS = {
    "José": "Hose", "Jose": "Hose", "Juan": "Huan", "Jorge": "Horhe", "Jesús": "Hesus", "Javier": "Hawjer",
    "Julio": "Hulio", "Carlos": "Karlos", "Miguel": "Migel", "Guillermo": "Gijermo", "Alejandro": "Alehandro",
    "Ramón": "Ramon", "Raúl": "Raul", "Joaquín": "Hoakin", "Joaquin": "Hoakin", "Cristina": "Kristina",
    "Carmen": "Karmen", "Guadalupe": "Gwadalupe", "Ximena": "Himena", "Lucía": "Lus'ija", "Sofía": "Sofija",
    "Valentina": "Walentina", "Camila": "Kamila", "Gustavo": "Gustawo", "Ernesto": "Ernesto", "Cortez": "Kortes",
    "Rodríguez": "Rodriges", "Rodriguez": "Rodriges", "Hernández": "Ernandes", "Hernandez": "Ernandes",
    "González": "Gonsales", "Gonzalez": "Gonsales", "Martínez": "Martines", "Martinez": "Martines",
    "López": "Lopes", "Lopez": "Lopes", "Pérez": "Peres", "Perez": "Peres", "Sánchez": "Sanczes",
    "Sanchez": "Sanczes", "Ramírez": "Ramires", "Ramirez": "Ramires", "Vargas": "Wargas", "Vega": "Wega",
    # wtrącenia
    "hola": "ola", "mija": "micha", "mijo": "micho", "hermano": "ermano", "hermana": "ermana", "chica": "czika",
    "chico": "cziko", "chicas": "czikas", "gracias": "grasjas", "vámonos": "wamonos", "vamonos": "wamonos",
    "vamos": "wamos", "cabrón": "kabron", "cabron": "kabron", "pendejo": "pendeho", "pendeja": "pendeha",
    "mierda": "mjerda", "señor": "senior", "señora": "seniora", "señorita": "seniorita", "qué": "ke",
    "sí": "si", "por favor": "por fawor", "jefe": "hefe", "carnal": "karnal", "loco": "loko", "loca": "loka",
    "cállate": "kajate", "ándale": "andale", "órale": "orale", "güey": "łej", "gringo": "gringo",
    "hijo": "iho", "hija": "iha", "mamá": "mama", "papá": "papa", "abuela": "abuela", "familia": "familja",
    "dinero": "dinero", "policía": "polis'ija", "cerveza": "serwesa", "bueno": "bueno", "claro": "klaro",
    "ay": "aj", "Dios": "Djos", "mío": "mijo", "mi amor": "mi amor", "cariño": "karinio", "querida": "kerida",
    "querido": "kerido", "perdón": "perdon", "adiós": "adjos", "buenas noches": "buenas noczes",
}
for _es, _pl in list(SPANISH_WORDS.items()):
    ENGLISH_NAMES[_es] = _pl
    if _es[:1].islower():  # wtrącenie na początku zdania: „Hola”, „Vamos”
        ENGLISH_NAMES[_es[:1].upper() + _es[1:]] = _pl[:1].upper() + _pl[1:]

# pozostałe słowa z hiszpańskimi znakami (á é í ú ñ ü — nie ma ich w polskim): ogólne reguły wymowy
_SPANISH_MARK = re.compile(r"[áéíúñüÁÉÍÚÑÜ]")
_SPANISH_RULES = [
    (r"ll", "j"), (r"ñ", "ni"), (r"qu(?=[eiéí])", "k"), (r"gu(?=[eiéí])", "g"), (r"gü", "gł"),
    (r"c(?=[eiéí])", "s"), (r"z", "s"), (r"j", "h"), (r"g(?=[eiéí])", "h"), (r"v", "w"), (r"ch", "cz"),
    (r"c", "k"), (r"^h", ""), (r"y$", "j"), (r"[áÁ]", "a"), (r"[éÉ]", "e"), (r"[íÍ]", "i"), (r"[óÓ]", "o"),
    (r"[úÚü]", "u"),
]


def _spanish_word(match):
    word = match.group(0)
    if not _SPANISH_MARK.search(word):
        return word
    out = word.lower()
    for pattern, repl in _SPANISH_RULES:
        out = re.sub(pattern, repl, out)
    out = hard_s(out)  # „policía” → „polis'ia”, nie „poliśia”
    return out[:1].upper() + out[1:] if word[:1].isupper() else out


def spanish_spoken(word):
    """Hiszpańskie imię/nazwisko → wymowa zapisana po polsku (García → Gars'ija, Juan → Huan)."""
    out = word.lower()
    out = re.sub(r"í(?=[aeiouáéóú])", "i\x02", out)  # akcent na „í”: osobna sylaba (Rocío → Rosijo)
    out = out.replace("ch", "\x01").replace("ll", "\x03").replace("ñ", "\x04")
    out = out.replace("h", "")  # hiszpańskie „h” jest nieme (poza „ch”)
    rules = [
        (r"qu(?=[eiéí])", "k"), (r"gu(?=[eiéí])", "\x05"), (r"gü", "gł"), (r"gu(?=[aoáó])", "gw"),
        (r"c(?=[eiéí])", "s"), (r"z", "s"), (r"j", "h"), (r"g(?=[eiéí])", "h"), (r"^x", "h"), (r"x", "ks"),
        (r"v", "w"), (r"c", "k"), (r"y$", "j"), (r"(^|[aeiouáéíóú])y(?=[aeiouáéíóú])", r"\1j"),
        (r"[áÁ]", "a"), (r"[éÉ]", "e"), (r"[íÍ]", "i"), (r"[óÓ]", "o"), (r"[úÚ]", "u"),
    ]
    for pattern, repl in rules:
        out = re.sub(pattern, repl, out)
    out = out.replace("\x01", "cz").replace("\x03", "j").replace("\x04", "ni").replace("\x05", "g")
    # po polsku „si” to „ś” — hiszpańskie brzmi jak „sj” przed samogłoską, „sy” przed spółgłoską
    out = re.sub(r"si(?=[aeiou])", "sj", out)
    out = re.sub(r"si(?![aeiouj\x02])", "sy", out)
    out = re.sub(r"ni(?=[aeiou])", "nj", out)
    out = out.replace("\x02", "j")
    return out[:1].upper() + out[1:]


# Hiszpańskie imiona i nazwiska (Vice City / Leonida, RDR2) — wymowa z reguł, z akcentami i bez.
# Pominięte te, które lektor i tak czyta dobrze albo są częste po angielsku (Julia, Daniel, David…).
SPANISH_NAMES = """
Alejandro Alberto Alfonso Alfredo Álvaro Andrés Ángel Antonio Armando Arturo Carlos César Cristian Diego
Eduardo Emilio Enrique Esteban Eugenio Felipe Fernando Francisco Gerardo Gonzalo Guillermo Gustavo Héctor
Ignacio Iván Jaime Javier Jesús Joaquín Jorge José Juan Julio Leonardo Lorenzo Luis Manuel Martín Mateo
Miguel Nicolás Óscar Pablo Pedro Rafael Ramón Raúl Ricardo Roberto Rodrigo Rubén Salvador Santiago Sergio
Tomás Vicente Víctor Adriana Alejandra Alicia Ángela Beatriz Carolina Catalina Cecilia Claudia Cristina
Daniela Dolores Elena Esperanza Fernanda Gabriela Graciela Guadalupe Inés Isabel Jimena Josefina Juana
Leticia Lorena Lucía Luisa Magdalena Marisol Mercedes Mónica Natalia Paloma Patricia Pilar Raquel Rocío
Rosario Silvia Sofía Teresa Valentina Valeria Verónica Ximena Yolanda Consuelo Soledad Maribel Marisa
Pepe Paco Chucho Nacho Lupe Chuy Beto Memo Toño Chela Conchita Maricela Araceli Yesenia Yadira
García Rodríguez Fernández González López Martínez Sánchez Pérez Gómez Jiménez Ruiz Hernández Díaz
Moreno Muñoz Álvarez Romero Alonso Gutiérrez Navarro Torres Domínguez Vázquez Ramos Ramírez Serrano
Blanco Molina Morales Suárez Ortega Delgado Castro Ortiz Rubio Marín Núñez Iglesias Medina Garrido
Cortés Castillo Santos Lozano Guerrero Cano Prieto Méndez Cruz Calvo Gallego Vidal León Márquez Herrera
Peña Flores Cabrera Campos Vega Fuentes Carrasco Caballero Reyes Nieto Aguilar Pascual Santana Herrero
Montero Hidalgo Giménez Ibáñez Ferrer Durán Benítez Mora Vargas Arias Carmona Crespo Román Soto Sáez
Velasco Moya Soler Parra Bravo Gallardo Rojas Mendoza Salazar Escobar Guzmán Villa Rivera Espinoza
Contreras Sandoval Figueroa Acosta Cárdenas Estrada Valdez Ochoa Zapata Montoya Quintero Orozco
Maldonado Cervantes Bautista Caminos Escuella Duarte Treviño Castañeda Villanueva Salinas Pacheco Ibarra
Robles Carrillo Barrera Cortez Velázquez Juárez Chávez Aguirre Mejía Cabello Sosa Rosales Solís Lara
""".split()
_ACCENT_FOLD = str.maketrans("áéíóúÁÉÍÓÚ", "aeiouAEIOU")
for _name in SPANISH_NAMES:
    for _form in {_name, _name.translate(_ACCENT_FOLD)}:
        if _form not in ENGLISH_NAMES:
            ENGLISH_NAMES[_form] = spanish_spoken(_name)

# Po polsku „si” brzmi jak „ś” (Trejsi → „Trejśi”, Wajs Siti → „Wajs Śiti”, Lusija → „Luśija”), a w imionach
# angielskich i hiszpańskich „s” jest twarde. Apostrof rozdziela głoski bez pauzy — zmierzone na lektorze:
# szum głoski ~5–5,6 kHz jak w „Kasa” zamiast ~3,5–3,9 kHz jak w „Kaśi”.


def hard_s(spoken):
    return re.sub(r"([sS])i", r"\1'i", spoken.replace("s'i", "si").replace("S'i", "Si"))


for _name, _spoken in list(ENGLISH_NAMES.items()):
    ENGLISH_NAMES[_name] = hard_s(_spoken)


# polskie końcówki odmiany doklejane do imienia (także po apostrofie: Mike'a, Tony'ego)
_PL_ENDINGS = "ami|ach|owi|owie|ów|om|em|zie|ie|ego|emu|iego|iemu|a|u|y|i|ii|ę|ą|o|e"


def _name_pattern():
    names = sorted(ENGLISH_NAMES, key=len, reverse=True)
    alts = []
    for name in names:
        stem = re.escape(name[:-1]) if name[-1:] in "ao" and " " not in name and len(name) >= 5 else None
        if stem and name.endswith("a"):  # Lucia → Lucii, Lucię, Lucią, Lucio
            alts.append(f"(?P<s{len(alts)}>{stem})(?:a|ii|i|ę|ą|o|y|e)")
        elif stem:  # Guillermo → Guillerma, Guillermem, Guillermowi
            alts.append(f"(?P<s{len(alts)}>{stem})(?:a|owi|em|ie|u)")
        alts.append(f"(?P<n{len(alts)}>{re.escape(name)})(?:'?(?:{_PL_ENDINGS}))?")
    return re.compile(r"(?<![\w'])(?:" + "|".join(alts) + r")(?![\w])")


_NAME_RE = _name_pattern()


def _name_sub(match):
    word = match.group(0)
    for key, value in match.groupdict().items():
        if value is None:
            continue
        if key.startswith("s"):  # rzeczownik na -a: podmień temat, zostaw polską końcówkę
            full = next(n for n in ENGLISH_NAMES if n[:-1] == value and n[-1:] in "ao" and len(n) >= 5)
            spoken = ENGLISH_NAMES[full][:-1]
            ending = word[len(value):]
        else:
            spoken = ENGLISH_NAMES[value]
            ending = word[len(value):].lstrip("'")
        if not ending:
            return ENGLISH_NAMES.get(value, spoken + "a") if key.startswith("n") else spoken + ending
        # Toni + ego → Toniego (nie „Toniiego”), Majk + a → Majka, Dżesik + y → Dżesiki
        if spoken.endswith("i") and ending.startswith("i"):
            ending = ending[1:]
        # Lus'ij + ii → Lus'iji (Lucii), nie „Lus'ijii”
        if spoken.endswith("j") and ending.startswith("ii"):
            ending = ending[1:]
        if spoken[-1:] in "kg" and ending.startswith("y"):
            ending = "i" + ending[1:]
        return spoken + ending
    return word


def english_names_pl(text):
    """Angielskie i hiszpańskie imiona/wtrącenia → ich wymowa zapisana po polsku, z zachowaniem odmiany."""
    text = _NAME_RE.sub(_name_sub, text or "")
    return re.sub(r"[A-Za-zÀ-ÿ]+", _spanish_word, text)


# Skracanie jak u lektora — TYLKO gdy kwestia nie zmieści się w czasie napisu nawet przy maks. tempie.
# Poziom 1: wtrącenia i powtórzenia. Poziom 2: dodatkowo imię w wołaczu na początku/końcu.
_CONDENSE_FILLERS = (
    "no wiesz", "wiesz co", "to znaczy", "tak naprawdę", "w sumie", "po prostu", "w ogóle", "no więc",
    "posłuchaj", "słuchaj", "wiesz", "stary", "stara", "kurczę", "kurde", "właściwie", "jakby", "no", "ej",
    "okej", "ok", "hej", "joł", "ej ty",
)
_FILLER_ALT = "|".join(re.escape(f) for f in sorted(_CONDENSE_FILLERS, key=len, reverse=True))
_CLAUSE_WORD = r"(?:że|żeby|co|kto|kogo|komu|gdzie|jak|czy|ile|dlaczego|kiedy|który|która|które|to)\b"
# wtrącenie na początku — ale nie „Wiesz, że…”, „Słuchaj, co…” (wtedy to czasownik i zdanie się sypie)
_FILLER_START = re.compile(rf"^(?:(?:{_FILLER_ALT})\b[,!.]?\s+(?!{_CLAUSE_WORD}))+", re.IGNORECASE)
_FILLER_MID = re.compile(rf",\s*(?:{_FILLER_ALT})\s*(?=,)", re.IGNORECASE)
_FILLER_END = re.compile(rf",\s*(?:{_FILLER_ALT})\s*(?=[.!?…]|$)", re.IGNORECASE)
_REPEAT = re.compile(r"\b(\w+)(?:[,\s]+\1\b)+", re.IGNORECASE)


_ONLY_FILLER = re.compile(rf"^(?:(?:{_FILLER_ALT})\b[\s,]*)+[.!?…]*$", re.IGNORECASE)


def _join_parts(srcs):
    """Kilka napisów → jedna wypowiedź (każdy zamknięty znakiem końca zdania)."""
    return " ".join(p if re.search(r"[.!?…]$", p) else p + "." for p in srcs)


def _only_names(sentence):
    words = re.findall(r"[\wÀ-ÿ']+", sentence)
    return bool(words) and all(w in ENGLISH_NAMES or w.lower() in _CONDENSE_FILLERS for w in words)


def condense_polish(text, level=1):
    """Skraca kwestię zdanie po zdaniu, bez zmiany sensu. Zwraca oryginał, gdyby zostało za mało."""
    original = normalize_text(text)
    kept = []
    for sentence in re.findall(r"[^.!?…]+(?:[.!?…]+|$)", original):
        sentence = sentence.strip()
        if not sentence:
            continue
        if _ONLY_FILLER.match(sentence):
            continue  # całe zdanie to wtrącenie („Słuchaj.”, „No.”) — wypada
        if level >= 2 and _only_names(sentence):
            continue  # samo wołanie („Raymond.”, „Jason!”) — przy dużym spóźnieniu wypada
        kept.append(_condense_sentence(sentence, level))
    out = normalize_text(" ".join(kept))
    if sum(ch.isalpha() for ch in out) < max(4, len(original) // 4):
        return original
    return out


def _condense_sentence(text, level=1):
    original = normalize_text(text)
    out = _REPEAT.sub(r"\1", original)
    out = _FILLER_START.sub("", out)
    out = _FILLER_MID.sub("", out)
    # „…, wiesz?” / „…, no nie?” — pytajnik należał do dopisku; zostaje, jeśli reszta sama jest pytaniem
    tag = re.search(r",\s*(?:wiesz|no nie|nie|prawda|tak)\s*\?+\s*$", out, flags=re.IGNORECASE)
    if tag:
        rest = out[: tag.start()]
        asks = re.match(r"(?i)\s*(?:co|kto|kim|kogo|komu|czego|gdzie|kiedy|dlaczego|czemu|jak|czy|ile|któr|jak)", rest)
        out = rest + ("?" if asks else ".")
    out = _FILLER_END.sub("", out)
    if level >= 2:
        names = "|".join(re.escape(n) for n in sorted(ENGLISH_NAMES, key=len, reverse=True) if n[:1].isupper())
        out = re.sub(rf"^(?:{names})\s*[,!]\s+", "", out)
        out = re.sub(rf",\s*(?:{names})\s*(?=[.!?…]|$)", "", out)
    out = normalize_text(re.sub(r"\s+([,.!?…])", r"\1", out))
    out = out[:1].upper() + out[1:]
    if sum(ch.isalpha() for ch in out) < max(4, len(original) // 4):
        return original
    return out


def polish_pronounce(text):
    for pattern, repl in _PRONOUNCE_RULES:
        text = pattern.sub(repl, text)
    return text


def lektor_short_speed_cap(text):
    """Górna granica tempa dla krótkich fragmentów — żeby model zdążył wymówić całe słowa."""
    letters = sum(ch.isalpha() for ch in text or "")
    if letters <= 8:
        return 0.9
    if letters <= 16:
        return 0.95
    if letters <= 28:
        return 1.02
    return LEKTOR_MAX_SPEED


def lektor_speed_split(pace, text):
    """Tempo fragmentu → (tempo modelu, przyspieszenie atempo w ffmpeg). pace < 1 = szybciej.

    Model czyta tylko tak szybko, jak umie wyraźnie (krótkie fragmenty wolniej, bo gubi głoski),
    a resztę tempa dociąga ffmpeg — bez ucinania słów i bez bełkotu."""
    want = max(0.9, min(LEKTOR_MAX_RATE, LEKTOR_SPEED / max(0.5, pace)))
    speed = min(want, LEKTOR_MAX_SPEED, lektor_short_speed_cap(text))
    return speed, min(LEKTOR_MAX_STRETCH, want / speed)


def lektor_pace(text):
    """Długie kwestie lekko szybciej, żeby lektor nadążał za napisami."""
    n = len(text or "")
    if n > 220:
        return 0.88
    if n > 140:
        return 0.93
    return 1.0


# Odtwarzacz w OSOBNYM procesie z ciągle otwartym strumieniem audio: fragment rusza od razu
# (bez startu afplay przy każdym fragmencie), a praca silnika (OCR, synteza) nie powoduje trzasków.
_PLAYER_CODE = r"""
import sys, threading, queue, wave
import numpy as np
import sounddevice as sd
SR = 44100
lock = threading.Lock()
cur = [None, 0, None]
done = queue.Queue()
def cb(out, frames, _t, _st):
    with lock:
        d = cur[0]
        if d is None:
            out.fill(0)
            return
        p = cur[1]
        n = min(frames, d.size - p)
        out[:n, 0] = d[p:p + n]
        out[n:, 0] = 0
        cur[1] = p + n
        if cur[1] >= d.size:
            done.put(cur[2])
            cur[0] = None
def writer():
    while True:
        sys.stdout.write("DONE %s\n" % done.get())
        sys.stdout.flush()
threading.Thread(target=writer, daemon=True).start()
def open_stream():
    st = sd.OutputStream(samplerate=SR, channels=1, dtype="float32", blocksize=1024, callback=cb)
    st.start()
    return st, sd.query_devices(kind="output")["name"]
stream, device = open_stream()
sys.stdout.write("READY\n"); sys.stdout.flush()
for line in sys.stdin:
    cmd, _, arg = line.strip().partition(" ")
    if cmd == "PLAY":
        pid, _, path = arg.partition(" ")
        try:
            with wave.open(path, "rb") as w:
                sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
            d = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if ch > 1:
                d = d.reshape(-1, ch).mean(axis=1)
            if sr != SR:
                d = np.interp(np.arange(0, d.size, sr / SR), np.arange(d.size), d).astype(np.float32)
        except Exception:
            done.put(pid)
            continue
        with lock:
            if cur[0] is not None:
                done.put(cur[2])
            cur[0], cur[1], cur[2] = d, 0, pid
    elif cmd == "STOP":
        with lock:
            if cur[0] is not None:
                done.put(cur[2])
                cur[0] = None
"""


class _PlayHandle:
    """Udaje Popen (poll/terminate/wait/kill) dla fragmentu grającego w procesie odtwarzacza."""

    def __init__(self, player, pid, seconds):
        self.player, self.pid, self.done = player, pid, threading.Event()
        # bezpiecznik: fragment nie może „grać” dłużej niż trwa (+1,5 s) — inaczej lektor by stanął
        self.deadline = time.monotonic() + seconds + 1.5

    def poll(self):
        if self.done.is_set() or not self.player.alive():
            return 0
        if time.monotonic() > self.deadline:
            log_timing("odtwarzacz nie zgłasza końca — przechodzę na afplay")
            self.player.failed = True
            self.done.set()
            return 0
        return None

    def terminate(self):
        self.player.send("STOP")
        self.done.wait(0.3)
        self.done.set()

    kill = terminate

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return 0


class _WinsoundHandle:
    """Windows: odtwarzanie przez winsound, udaje Popen (poll/terminate/wait/kill)."""

    def __init__(self, path):
        import winsound

        self._winsound = winsound
        try:
            with wave.open(str(path), "rb") as info:
                seconds = info.getnframes() / float(info.getframerate())
        except Exception:
            seconds = 3.0
        self.end = time.monotonic() + seconds + 0.05
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    def poll(self):
        return 0 if time.monotonic() >= self.end else None

    def terminate(self):
        self._winsound.PlaySound(None, 0)
        self.end = 0.0

    kill = terminate

    def wait(self, timeout=None):
        left = self.end - time.monotonic()
        if left > 0:
            time.sleep(min(left, timeout) if timeout is not None else left)
        return 0


class PlayerProcess:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.handles = {}
        self.counter = 0
        self.failed = False

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        self.proc = subprocess.Popen(
            # -X utf8: ścieżka do pliku z nazwą użytkownika z polskimi znakami (Windows) przechodzi bez strat
            [sys.executable, "-X", "utf8", "-u", "-c", _PLAYER_CODE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            **NO_WINDOW,
        )
        if self.proc.stdout.readline().strip() != "READY":
            self.proc.kill()
            raise RuntimeError("odtwarzacz nie wstał")
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            if line.startswith("DONE "):
                handle = self.handles.pop(line[5:].strip(), None)
                if handle is not None:
                    handle.done.set()
        for handle in list(self.handles.values()):
            handle.done.set()
        self.handles.clear()

    def send(self, line):
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def play(self, path):
        with self.lock:
            if self.failed:
                return None
            try:
                if not self.alive():
                    self._start()
            except Exception:
                self.failed = True  # brak sounddevice/urządzenia — zostaje afplay
                return None
            self.counter += 1
            pid = str(self.counter)
            try:
                with wave.open(str(path), "rb") as info:
                    seconds = info.getnframes() / float(info.getframerate())
            except Exception:
                seconds = 10.0
            handle = _PlayHandle(self, pid, seconds)
            self.handles[pid] = handle
            self.send(f"PLAY {pid} {path}")
            return handle


TIMING_LOG = os.path.join(tempfile.gettempdir(), "livedub-engine.log") if IS_WIN else "/tmp/livedub-engine.log"


def log_timing(line):
    """Czasy lektora do /tmp/livedub-engine.log (Windows: %TEMP%\\livedub-engine.log) — do szukania opóźnień."""
    try:
        with open(TIMING_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass


# VoiceStudio (OmniVoice) na tym Macu: lektor z „voicepacka” — wzorcowego głosu wygenerowanego z opisu.
# Model działa w osobnym procesie Pythonem z VoiceStudio (torch na GPU Maca), ładuje się raz.
VOICESTUDIO_ROOT = Path(os.environ.get("VOICESTUDIO_ROOT") or Path.home() / "voice-studio-projekty/VoiceStudio")
VOICEPACK_DIR = CONFIG_PATH.parent / "voicepacks"
OMNI_STEPS = 8  # z krótkim wzorcem (~3 s) 8 kroków = ~0,6–0,8 s na kwestię
OMNI_TIMEOUT = 8.0
# OmniVoice z natury czyta ~18 zn/s — lektor filmowy ~15–16 zn/s
OMNI_SPEED_SCALE = 0.87


def voicepacks():
    try:
        return sorted(p.name for p in VOICEPACK_DIR.iterdir() if (p / "voice.wav").is_file() and (p / "voice.txt").is_file())
    except OSError:
        return []


def voicestudio_python():
    py = VOICESTUDIO_ROOT / ".venv/bin/python"
    return py if not IS_WIN and py.exists() else None


class OmniVoiceLocal:
    rate = 24000

    def __init__(self, pack):
        self.pack = re.sub(r"[^\w\-]", "", pack or "")
        self.voice = self.pack
        # „pogrubienie” z voice.json: ton i barwa w dół (0,92 = -8 %), długość bez zmian
        try:
            meta = json.loads((VOICEPACK_DIR / self.pack / "voice.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        deepen = max(0.8, min(1.0, float(meta.get("deepen") or 1.0)))
        self.pre_filter = (
            f"asetrate={self.rate * deepen:.0f},aresample={self.rate},atempo={1 / deepen:.4f}" if deepen < 0.999 else ""
        )
        self.tag = f"ov1|{self.pack}|{OMNI_STEPS}|{deepen:.2f}|{OMNI_SPEED_SCALE}"
        self.error = ""
        self.failed_until = 0.0
        self.ready = False
        self.proc = None
        self.lock = threading.Lock()
        self._n = 0
        self.on_ready = None
        threading.Thread(target=self._start, daemon=True).start()

    def _start(self):
        py = voicestudio_python()
        script = Path(__file__).resolve().with_name("omnivoice_sidecar.py")
        if py is None or not script.exists() or self.pack not in voicepacks():
            self.error = "VoiceStudio: brak VoiceStudio albo voicepacka — czytam Supertonic."
            self.failed_until = float("inf")
            return
        with self.lock:
            try:
                # silnik LiveDub działa z PYTHONHOME/PYTHONPATH swojego Pythona — VoiceStudio ma własny venv
                # i z tymi zmiennymi pada od razu (sesja 16:33: „brak odpowiedzi” po 0,07 s)
                env = {k: v for k, v in os.environ.items()
                       if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONPYCACHEPREFIX")}
                self.proc = subprocess.Popen(
                    [str(py), str(script), str(VOICESTUDIO_ROOT), str(VOICEPACK_DIR / self.pack), str(OMNI_STEPS)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    cwd=str(VOICESTUDIO_ROOT), bufsize=0, env=env,
                )
                hello = json.loads(self.proc.stdout.readline() or b"{}")
            except Exception as exc:
                hello = {"error": str(exc)}
        if hello.get("ready"):
            self.rate = int(hello.get("rate") or self.rate)
            self.ready = True
            log_timing(f"VoiceStudio gotowy ({self.pack}, {hello.get('device')})")
        else:
            self.error = f"VoiceStudio nie wstaje: {hello.get('error') or 'brak odpowiedzi'} — czytam Supertonic."
            self.failed_until = float("inf")
            self.close()
        if self.on_ready:
            self.on_ready(self)

    def synth(self, text, speed=1.0, previous=""):
        """Mono float32 albo None (model jeszcze się ładuje / padł) — wtedy lektor czyta Supertonic."""
        import select

        if not self.ready or time.monotonic() < self.failed_until:
            return None
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self.ready = False
                self.error = "VoiceStudio: proces lektora padł — czytam Supertonic."
                self.failed_until = float("inf")
                return None
            self._n += 1
            out = CACHE_DIR / f"ov_job_{os.getpid()}_{self._n % 4}.wav"
            speed = max(0.75, min(1.2, speed * OMNI_SPEED_SCALE))
            job = {"id": self._n, "text": text, "speed": round(speed, 2), "out": str(out)}
            try:
                proc.stdin.write((json.dumps(job, ensure_ascii=False) + "\n").encode("utf-8"))
                proc.stdin.flush()
                ready, _w, _x = select.select([proc.stdout], [], [], OMNI_TIMEOUT)
                answer = json.loads(proc.stdout.readline() or b"{}") if ready else {"error": "za długo"}
            except Exception as exc:
                answer = {"error": str(exc)}
            if not answer.get("ok"):
                self.error = f"VoiceStudio: {answer.get('error') or 'błąd'} — ta kwestia Supertonic."
                if not ready:
                    self.close()  # zawieszony model: zabij, żeby nie trzymał GPU
                    self.failed_until = float("inf")
                return None
        try:
            with wave.open(str(out), "rb") as handle:
                pcm = handle.readframes(handle.getnframes())
        except Exception:
            return None
        self.error = ""
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

    def close(self):
        proc, self.proc = self.proc, None
        self.ready = False
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _cap_onnx_arena(st_loader):
    """Wyłącz arenę ONNX Runtime — inaczej każdy nowy rozmiar tekstu dokłada blok, którego nikt nie zwalnia."""
    real = getattr(st_loader.ort, "InferenceSession", None)
    if real is None or getattr(real, "_livedub_capped", False):
        return

    # podklasa, nie funkcja: Supertonic sprawdza isinstance(sesja, ort.InferenceSession)
    class CappedSession(real):
        _livedub_capped = True

        def __init__(self, path, sess_options=None, providers=None, **kwargs):
            opts = sess_options if sess_options is not None else st_loader.ort.SessionOptions()
            opts.enable_cpu_mem_arena = False
            opts.enable_mem_pattern = False
            super().__init__(path, sess_options=opts, providers=providers, **kwargs)

    st_loader.ort.InferenceSession = CappedSession


class MaleLektor:
    """Lektor filmowy: Supertonic 3 na GPU (CoreML), równy głos, zmasterowany przez ffmpeg."""

    def __init__(self):
        self.player = None
        self.lock = threading.Lock()
        self.synth_lock = threading.Lock()
        self.model = None
        self.styles = {}
        self.voice = DEFAULT_SUPERTONIC_VOICE
        self.tts_scale = 1.0
        self.device_label = "GPU"
        self.load_lock = threading.Lock()
        # do kiedy lektor jeszcze mówi (szacunek) — kwestie przygotowywane zawczasu liczą z tym czasem
        self.busy_until = 0.0
        self.cps1 = LEKTOR_CPS_PRIOR
        self.out = PlayerProcess()
        self.ffmpeg = which_bin("ffmpeg") or bundled_ffmpeg()
        self.cloud = None  # OmniVoiceLocal, gdy wybrany lektor z VoiceStudio
        self._cloud_prev = ("", 0.0)
        self.on_cloud_error = None

    def set_backend(self, backend):
        """OmniVoiceLocal albo None (Supertonic). Poprzedni lokalny model zwalnia GPU."""
        old, self.cloud = self.cloud, backend
        if old is not None and old is not backend and hasattr(old, "close"):
            old.close()

    @property
    def backend(self):
        return "supertonic" if self.model is not None else None

    def ensure(self):
        if self.model is not None:
            return
        with self.load_lock:
            if self.model is None:
                self._load()

    def _load(self):
        import onnxruntime
        import supertonic.loader as st_loader
        from supertonic import TTS

        onnxruntime.set_default_logger_severity(3)
        if self.voice not in SUPERTONIC_VOICES:
            self.voice = DEFAULT_SUPERTONIC_VOICE

        def load(providers):
            st_loader.DEFAULT_ONNX_PROVIDERS = providers
            # arena ONNX pamięta każdy rozmiar kwestii i nie oddaje RAM; przy zmiennej długości
            # napisów potrafi urosnąć do dziesiątek GB
            _cap_onnx_arena(st_loader)
            tts = TTS(auto_download=True)
            style = tts.get_voice_style(voice_name=self.voice)
            tts.synthesize("Lektor gotowy.", voice_style=style, lang="pl")  # rozgrzewka
            return tts, style

        # CPU: w pomiarach na Macu szybsze od CoreML (GPU) — 1,4–1,8 s wobec 1,6–3,0 s na 3 zdania,
        # a CoreML dodatkowo przelicza się przy nowych długościach tekstu. GPU tylko jako zapas.
        try:
            model, calm = load(["CPUExecutionProvider"])
            self.device_label = "CPU"
        except Exception as exc:
            log_timing(f"lektor: CPU nie wstaje ({exc}) — próbuję GPU")
            model, calm = load(["CoreMLExecutionProvider", "CPUExecutionProvider"])
            self.device_label = "GPU"
        # M1 = żywszy głos; domieszka daje więcej melodii przy tej samej barwie lektora
        other = model.get_voice_style(voice_name="M1" if self.voice != "M1" else "M4")
        # proces odtwarzacza od razu — pierwsza kwestia nie czeka na jego start
        with self.lock:
            try:
                if not self.out.alive() and not self.out.failed:
                    self.out._start()
            except Exception:
                self.out.failed = True
        self.styles = {"calm": calm, "lively": other}
        self.model = model

    def stop(self):
        with self.lock:
            if self.player and self.player.poll() is None:
                self.player.terminate()
                try:
                    self.player.wait(timeout=0.4)
                except Exception:
                    try:
                        self.player.kill()
                    except Exception:
                        pass
            self.player = None

    def wait(self, interrupt_check=None, poll=0.04):
        while True:
            with self.lock:
                proc = self.player
            if proc is None or proc.poll() is not None:
                return False
            if interrupt_check is not None and interrupt_check():
                self.stop()
                return True
            time.sleep(poll)

    def plan(self, text):
        return lektor_segments(text)

    def overload(self, text, seconds):
        """Ile razy za szybko musiałby czytać lektor, żeby zmieścić się w `seconds` (>1 = nie zdąży)."""
        if not seconds or seconds <= 0:
            return 0.0
        # sufit łącznego tempa lektora (nie samego modelu) — przy ×1,15 skrót wchodzi, zanim narośnie spóźnienie
        return len(text or "") / (self.cps1 * max(0.6, seconds * 0.92)) / LEKTOR_MAX_RATE

    def line_boost(self, text, seconds):
        """Przyspieszenie CAŁEJ kwestii (jedno dla wszystkich jej fragmentów).

        Dłuższa kwestia = szybciej (napis i tak zniknie), a do tego lektor ma się zmieścić
        w `seconds` — czasie, przez jaki gra pokazuje taki napis."""
        base = LEKTOR_SPEED / max(0.5, float(self.tts_scale or 1.0))
        n = len(text or "")
        if n <= 60:
            by_length = 1.0
        elif n <= 120:
            by_length = 1.0 + 0.08 * (n - 60) / 60
        elif n <= 200:
            by_length = 1.08 + 0.08 * (n - 120) / 80
        else:
            by_length = 1.16
        needed = len(text) / (self.cps1 * max(0.6, seconds * 0.92)) / base if seconds and seconds > 0 else 1.0
        boost = max(1.0, by_length, min(LEKTOR_MAX_RATE / base, needed))
        boost = min(boost, LEKTOR_MAX_RATE / base)
        return round(boost * 20) / 20  # stopnie co 0,05 — cache się powtarza

    def _style(self, blend):
        if blend <= 0.0:
            return self.styles["calm"]
        calm, other = self.styles["calm"], self.styles["lively"]
        style = copy.copy(calm)
        style.ttl = (1 - blend) * calm.ttl + blend * other.ttl
        style.dp = (1 - blend) * calm.dp + blend * other.dp
        return style

    def prepare(self, text, volume=1.0, mood="calm", arousal=0.0, hurry=False, boost=None):
        """Zwraca ścieżkę gotowego WAV (z cache, jeśli ta sama kwestia już była).

        arousal: jak mówi postać (-1 szept … +1 krzyk), mood: wskazówka z tekstu."""
        self.ensure()
        a = max(-1.0, min(1.0, float(arousal or 0.0)))
        a = round(a * 5) / 5  # stopnie co 0,2 — cache się powtarza
        params = lektor_voice_params(a)
        # tempo całej kwestii przychodzi z line_boost(); bez niego — z długości tego fragmentu
        length_pace = lektor_pace(text) if boost is None else 1.0
        pace = length_pace * float(self.tts_scale or 1.0) / params["speed"]
        # interpunkcja napisu: „!” głośniej, „…” ciszej, pauza po „?” i „…”
        punct_gain, punct_pause = lektor_punctuation(text, mood)
        pause = max(params["pause"], punct_pause)
        if text.rstrip().endswith(","):
            pause = 0.0  # fragment ucięty na przecinku — reszta zdania leci od razu
        # boost: tempo całej kwestii dopasowane do tego, jak długo napis wisi na ekranie
        pace /= max(1.0, float(boost or 1.0))
        if hurry:
            # następny napis już czeka: ten fragment syntezuje się szybciej i bez pauzy
            pace /= LEKTOR_CATCHUP_RATE
            pause = min(pause, 0.03)
        # suwak Głośność = głośność lektora; przejęcie i interpunkcja ją modulują
        # volume 0…1 (suwak 0–100 %); 100 % = 1,3× — limiter i tak nie przepuści przesteru
        gain = 1.3 * max(0.0, min(1.0, float(volume))) * params["gain"] * punct_gain
        cloud = self.cloud
        voice_tag = cloud.tag if cloud is not None and time.monotonic() >= cloud.failed_until else f"st15|{self.voice}"
        key = f"{voice_tag}|{a:.1f}|{text}|{pace:.2f}|{gain:.2f}|{pause:.2f}|{bool(self.ffmpeg)}"
        path = CACHE_DIR / f"{text_key(key)}.wav"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= 64:
            return path
        raw = Path(str(path) + ".raw.wav")
        t0 = time.monotonic()
        stretch = None
        if not voice_tag.startswith("st"):
            stretch = self._synth_cloud(cloud, text, raw, pace, pause)
            if stretch is None:
                path = CACHE_DIR / f"{text_key(key.replace(voice_tag, f'st15|{self.voice}', 1))}.wav"
                if path.exists() and path.stat().st_size >= 64:
                    return path
        filt = LEKTOR_FILTER
        if stretch is not None:
            pre = getattr(cloud, "pre_filter", "")
            filt = f"{pre},{LEKTOR_CLOUD_FILTER}" if pre else LEKTOR_CLOUD_FILTER
        if stretch is None:
            stretch = self._synth(text, raw, pace, self._style(params["blend"]), pause)
        t1 = time.monotonic()
        self._master(raw, path, gain, stretch, filt)
        t2 = time.monotonic()
        try:
            with wave.open(str(path), "rb") as handle:
                seconds = handle.getnframes() / float(handle.getframerate())
        except Exception:
            seconds = 0.0
        self.last_timing = (
            f"synteza {t1 - t0:.2f}s, ffmpeg {t2 - t1:.2f}s, nagranie {seconds:.2f}s "
            f"({len(text)} zn. → {len(text) / max(seconds, 0.1):.1f} zn/s)"
            + (f", atempo x{stretch:.2f}" if stretch > 1.01 else "")
        )
        log_timing(f"  fragment: {self.last_timing} | {text[:50]!r}")
        return path

    def _synth(self, text, path, pace, style, pause):
        # krótka kwestia („Do środka.”, „Mordo…”, „Dobra, chwila.”): Supertonic daje jej za mało czasu
        # i przy przyspieszeniu gubi ostatnie głoski albo całe słowo — model czyta ją wolniej,
        # a do tempa lektora dociąga ją ffmpeg (stretch)
        speed, stretch = lektor_speed_split(pace, text)
        spoken = polish_pronounce(english_names_pl(text))
        # bez kropki na końcu model potrafi urwać ostatnie słowo
        if not re.search(r"[.!?…,;:]\W*$", spoken):
            spoken = spoken.rstrip() + "."
        with self.synth_lock:
            wav, _dur = self.model.synthesize(
                spoken, voice_style=style, total_steps=SUPERTONIC_STEPS, speed=speed, lang="pl"
            )
        self._write_voice(np.asarray(wav, dtype=np.float32).reshape(-1), int(self.model.sample_rate), text, speed, path, pause)
        return stretch

    def _synth_cloud(self, cloud, text, path, pace, pause):
        """Lektor z VoiceStudio; None = nie wyszło (lektor czyta Supertonic). Tempo w całości po stronie głosu."""
        speed, stretch = lektor_speed_split(pace, text)
        rate = speed * stretch / LEKTOR_SPEED  # 1,0 = zwykłe tempo lektora
        prev, at = self._cloud_prev
        audio = cloud.synth(text, speed=rate, previous=prev if time.monotonic() - at < 12.0 else "")
        if audio is None:
            if cloud.error and self.on_cloud_error:
                self.on_cloud_error(cloud.error)
            return None
        self._cloud_prev = (text, time.monotonic())
        self._write_voice(audio, cloud.rate, text, max(0.5, rate * LEKTOR_SPEED), path, pause)
        return 1.0

    def _write_voice(self, audio, sr, text, speed, path, pause):
        # przytnij ciszę na brzegach (szybszy start), dodaj pauzę zależną od nastroju
        # próg niski i zapas na końcu: ciche „dź”, „ś”, „ć” nie mogą zostać ucięte
        loud = np.flatnonzero(np.abs(audio) > 0.004)
        if loud.size:
            # zapas na końcu 0,15 s: ciche końcówki („-ś”, „-ć”, „-dź”) nie mogą zostać ucięte
            audio = audio[max(0, loud[0] - int(sr * 0.04)) : loud[-1] + int(sr * 0.15)]
            # ucz się, ile znaków na sekundę czyta lektor (przy speed=1) — do dopasowania tempa
            spoken = (loud[-1] - loud[0]) / float(sr)
            if spoken > 0.6 and len(text) >= 12:
                cps1 = len(text) / spoken / max(0.5, speed)
                if 6.0 < cps1 < 30.0:
                    self.cps1 += 0.2 * (cps1 - self.cps1)
        # wyrównaj poziom (RMS części z głosem), bez przekraczania szczytu
        voiced = audio[np.abs(audio) > 0.01]
        if voiced.size:
            rms = float(np.sqrt(np.mean(voiced**2)))
            peak = float(np.max(np.abs(audio)))
            audio = audio * min(LEKTOR_TARGET_RMS / max(rms, 1e-4), 0.95 / max(peak, 1e-4))
        audio = np.concatenate([audio, np.zeros(int(sr * pause), dtype=np.float32)])
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(int(sr))
            handle.writeframes(pcm.tobytes())

    def _master(self, src, dst, gain, stretch=1.0, filt=LEKTOR_FILTER):
        tmp = Path(str(dst) + ".part.wav")
        chain = f"{filt},volume={gain:.2f},{LEKTOR_LIMITER}"
        if stretch > 1.01:
            chain = f"atempo={stretch:.3f},{chain}"
        try:
            if self.ffmpeg:
                result = subprocess.run(
                    [
                        self.ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
                        "-af", chain,
                        "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(tmp),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    **NO_WINDOW,
                )
                if result.returncode == 0 and tmp.is_file() and tmp.stat().st_size >= 64:
                    tmp.replace(dst)
                    return
            # bez ffmpeg: surowy głos (i tak czytelny)
            shutil.copyfile(src, dst)
        finally:
            for leftover in (src, tmp):
                try:
                    leftover.unlink()
                except OSError:
                    pass

    def play(self, path, rate=1.0):
        # afplay w osobnym procesie: OCR i synteza w silniku nie przerywają dźwięku (bez trzasków);
        # -r = szybsze odtwarzanie bez zmiany wysokości głosu (nadrabianie zaległości)
        with self.lock:
            handle = self.out.play(path)
            if handle is not None:
                self.player = handle
                return
        if IS_WIN:
            # zapas bez procesu odtwarzacza: winsound (bez zmiany tempa)
            with self.lock:
                self.player = _WinsoundHandle(path)
            return
        cmd = ["afplay", str(path)]
        if rate and abs(rate - 1.0) > 0.01:
            cmd = ["afplay", "-r", f"{rate:.2f}", "-q", "1", str(path)]
        with self.lock:
            self.player = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


class ArgosTranslator:
    def __init__(self):
        self._fn = None
        self._cache = {}
        self._lock = threading.Lock()

    def ensure(self):
        if self._fn is not None:
            return
        with HEAVY_IMPORT_LOCK:
            self._ensure()

    def _ensure(self):
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
        # napisy i dialogi się powtarzają — drugi raz bez czekania na tłumacza
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        with self._lock:
            self.ensure()
            out = normalize_text(self._fn(text))
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[text] = out
        return out


class ParakeetSTT:
    """Parakeet v3 na MLX. MLX trzyma strumienie obliczeń per wątek, a wagi ładuje leniwie —
    więc WSZYSTKO (wczytanie, rozgrzewka, transkrypcja) idzie przez jeden stały wątek."""

    def __init__(self):
        self.model = None
        self._jobs = queue.Queue()
        self._thread = None
        self._thread_lock = threading.Lock()

    def _call(self, fn, *args):
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="mlx-stt", daemon=True)
                self._thread.start()
        done = threading.Event()
        box = {}
        self._jobs.put((fn, args, box, done))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _loop(self):
        # limit zanim Parakeet w ogóle zaalokuje — inaczej cache MLX rośnie do ~55 GB
        cap_mlx_cache()
        try:
            import mlx.core as mx

            mx.set_default_device(mx.gpu)
        except Exception:
            pass
        while True:
            fn, args, box, done = self._jobs.get()
            try:
                box["result"] = fn(*args)
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()

    def ensure(self):
        if self.model is None:
            self._call(self._ensure)

    def warmup(self):
        self._call(self._warmup)

    def transcribe(self, audio):
        return self._call(self._transcribe, audio)

    # --- poniżej tylko w wątku mlx-stt ---

    def _ensure(self):
        if self.model is not None:
            return
        import mlx.core as mx
        from parakeet_mlx import from_pretrained

        model = from_pretrained(PARAKEET_MODEL)
        # wagi do pamięci od razu — leniwe tablice nie mogą czekać na inny wątek
        mx.eval(model.parameters())
        self.model = model

    def _warmup(self):
        self._ensure()
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        dummy = mx.array(np.zeros(int(SAMPLE_RATE * 0.7), dtype=np.float32))
        self.model.generate(get_logmel(dummy, self.model.preprocessor_config))

    @staticmethod
    def _norm_audio(audio):
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            audio = audio / peak
        return audio

    def _transcribe(self, audio):
        self._ensure()
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        audio = self._norm_audio(audio)
        if audio.size < int(SAMPLE_RATE * 0.2):
            return ""
        results = self.model.generate(get_logmel(mx.array(audio), self.model.preprocessor_config))
        text = ""
        if results:
            text = normalize_text(getattr(results[0], "text", None) or "")
        del results
        mx.clear_cache()
        return text


class ProsodyMeter:
    """Jak mówi postać: głośność, melodia i tempo względem typowej mowy w tej grze → „przejęcie” -1…1."""

    FRAME = 512  # 32 ms przy 16 kHz

    def __init__(self):
        self.ring = np.zeros(int(SAMPLE_RATE * 3.0), dtype=np.float32)
        self.ring_n = 0
        self.ring_t = 0.0
        self.lock = threading.Lock()
        # bazowe statystyki mowy w grze (średnia/odchylenie z wygaszaniem)
        self.stats = {}
        self.count = 0

    def feed(self, mono):
        if mono is None or mono.size == 0:
            return
        with self.lock:
            n = min(mono.size, self.ring.size)
            self.ring = np.roll(self.ring, -n)
            self.ring[-n:] = mono[-n:]
            self.ring_n = min(self.ring.size, self.ring_n + n)
            self.ring_t = time.monotonic()

    def recent(self, window=1.6):
        """Przejęcie z ostatnich sekund dźwięku gry (dla napisów); None gdy brak świeżej mowy."""
        with self.lock:
            if time.monotonic() - self.ring_t > 2.0 or self.ring_n < SAMPLE_RATE * 0.5:
                return None
            audio = self.ring[-int(SAMPLE_RATE * window) :].copy()
        return self.analyze(audio, learn=False)

    def _features(self, audio):
        frame = self.FRAME
        n = audio.size // frame
        if n < 8:
            return None
        frames = audio[: n * frame].reshape(n, frame).astype(np.float64)
        rms = np.sqrt(np.mean(frames**2, axis=1))
        gate = max(SPEECH_RMS * 0.6, float(np.percentile(rms, 60)) * 0.5)
        f0s = []
        voiced = []
        lo, hi = SAMPLE_RATE // 400, SAMPLE_RATE // 55
        for i in range(n):
            if rms[i] < gate:
                continue
            x = frames[i] - frames[i].mean()
            spec = np.fft.rfft(x, 2 * frame)
            ac = np.fft.irfft(spec * np.conj(spec))[:frame]
            if ac[0] <= 0:
                continue
            k = lo + int(np.argmax(ac[lo:hi]))
            if ac[k] >= 0.45 * ac[0]:
                f0s.append(SAMPLE_RATE / k)
                voiced.append(i)
        if len(f0s) < 5:
            return None
        semis = 12.0 * np.log2(np.asarray(f0s) / 100.0)
        # mediana z 5 ramek zjada skoki o oktawę; rozrzut odporny (IQR) zamiast odchylenia
        if semis.size >= 5:
            pad = np.pad(semis, 2, mode="edge")
            semis = np.median(np.lib.stride_tricks.sliding_window_view(pad, 5), axis=1)
        q75, q25 = np.percentile(semis, [75, 25])
        # tempo: szczyty obwiedni wygładzonej ~160 ms, wyraźnie ponad dolinami
        env = np.convolve(rms, np.ones(5) / 5.0, mode="same")
        mid = env[1:-1]
        peaks = (mid > env[:-2]) & (mid >= env[2:]) & (mid > gate * 1.5)
        idx = np.flatnonzero(peaks) + 1
        count = 0
        last = -99
        for i in idx:
            if i - last >= 4:  # min. ~130 ms między sylabami
                count += 1
                last = i
        dur = max(0.3, n * frame / SAMPLE_RATE)
        return {
            "energy": float(np.log(np.mean(rms[voiced]) + 1e-6)),
            "pitch": float(np.median(semis)),
            "melody": float((q75 - q25) / 1.35),
            "rate": float(count / dur),
        }

    # priorytety zanim poznamy grę: melodia/tempo w wartościach bezwzględnych
    PRIORS = {"melody": (2.2, 1.0), "rate": (3.0, 1.0)}
    WEIGHTS = {"energy": 0.45, "melody": 0.35, "rate": 0.2, "pitch": 0.2}

    def is_quiet(self, audio):
        """Cicha wypowiedź (postać mruczy pod nosem, tłum w tle) — względem typowej mowy w tej grze."""
        audio = np.asarray(audio, dtype=np.float32)
        feats = self._features(audio)
        if feats is None:
            # za mało wyraźnej mowy, żeby ją zmierzyć — traktuj jak tło
            return True
        mean, var = self.stats.get("energy", (None, None))
        if self.count >= 3 and mean is not None:
            return (feats["energy"] - mean) / max(np.sqrt(var), 0.15) < -1.2
        return float(np.exp(feats["energy"])) < SPEECH_RMS * 4

    def analyze(self, audio, learn=True):
        feats = self._features(np.asarray(audio, dtype=np.float32))
        if feats is None:
            return None
        score = 0.0
        for name, value in feats.items():
            mean, var = self.stats.get(name, (None, None))
            if self.count >= 3 and mean is not None:
                z = (value - mean) / max(np.sqrt(var), 0.15)
            elif name in self.PRIORS:
                pm, ps = self.PRIORS[name]
                z = (value - pm) / ps
            else:
                z = 0.0
            score += self.WEIGHTS[name] * max(-2.5, min(2.5, z))
        if learn:
            alpha = 0.5 if self.count < 3 else 0.12
            for name, value in feats.items():
                mean, var = self.stats.get(name, (value, 1.0))
                diff = value - mean
                mean += alpha * diff
                var = (1 - alpha) * (var + alpha * diff * diff)
                self.stats[name] = (mean, max(var, 0.02))
            self.count += 1
        return max(-1.0, min(1.0, score / 1.2))


def _periodic_frame(frame, threshold=0.35, strict=False):
    """Ramka okresowa w paśmie głosu (55–400 Hz) — głos tak, szum i wybuchy nie."""
    n = frame.size
    x = frame.astype(np.float64) - float(frame.mean())
    spec = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    if ac[0] <= 0:
        return False
    lo, hi = SAMPLE_RATE // 400, min(n - 1, SAMPLE_RATE // 55)
    k = lo + int(np.argmax(ac[lo:hi]))
    if ac[k] < threshold * float(ac[0]):
        return False
    if not strict:
        return True
    # prawdziwy okres = szczyt z dołkiem przed nim; buczenie ruchu ulicznego (niskie częstotliwości)
    # daje autokorelację opadającą gładko — maksimum na brzegu zakresu, bez dołka
    return k > lo + 2 and float(np.min(ac[lo:k])) < float(ac[k]) - 0.2 * float(ac[0])


def voiced_fraction(audio, frame=512):
    """Jaka część głośnych ramek wypowiedzi brzmi jak głos (a nie muzyka, szum czy wybuch)."""
    audio = np.asarray(audio, dtype=np.float32)
    n = audio.size // frame
    if n < 4:
        return 0.0
    frames = audio[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    loud = rms > max(SPEECH_RMS, float(np.percentile(rms, 30)))
    if not loud.any():
        return 0.0
    return sum(_periodic_frame(f) for f in frames[loud]) / float(n)


def sung_fraction(audio, frame=512, run=6, tol=0.6):
    """Jaka część głosu leży w długich (≥ ~190 ms) nutach o stałej wysokości — śpiew tak, mowa nie."""
    audio = np.asarray(audio, dtype=np.float32)
    lo, hi = SAMPLE_RATE // 400, SAMPLE_RATE // 70
    semis = []
    for i in range(audio.size // frame):
        x = audio[i * frame : (i + 1) * frame].astype(np.float64)
        x -= x.mean()
        if np.sqrt(np.mean(x**2)) < SPEECH_RMS:
            semis.append(None)
            continue
        spec = np.fft.rfft(x, 2 * frame)
        ac = np.fft.irfft(spec * np.conj(spec))[:frame]
        k = lo + int(np.argmax(ac[lo:hi]))
        semis.append(12.0 * np.log2(SAMPLE_RATE / k / 100.0) if ac[0] > 0 and ac[k] >= 0.35 * ac[0] else None)
    voiced = sum(v is not None for v in semis)
    if voiced < 8:
        return 0.0
    stable = i = 0
    while i < len(semis):
        if semis[i] is None:
            i += 1
            continue
        j = i
        while j + 1 < len(semis) and semis[j + 1] is not None and abs(semis[j + 1] - float(np.median(semis[i : j + 2]))) <= tol:
            j += 1
        if j - i + 1 >= run:
            stable += j - i + 1
        i = j + 1
    return stable / voiced


def is_full_sentence(text):
    """Rozpoznana mowa to pełne zdanie (co najmniej 4 słowa bez wtrąceń i koniec zdania albo 6+ słów)."""
    raw = normalize_text(text)
    words = [w for w in re.findall(r"[A-Za-z']+", raw) if not _is_filler_word(w)]
    if len(words) < 4 or raw.lower().strip(" .!?") in JUNK_HEARD:
        return False
    return bool(re.search(r"[.!?]\W*$", raw)) or len(words) >= 6


class VoiceActivity:
    """Czy postać teraz mówi (dźwięk gry) — żeby lektor wchodził jak w filmie, chwilę po oryginale.

    Ramka 32 ms z głosem = wyraźnie ponad tłem ORAZ okresowa w paśmie głosu (55–400 Hz); szum
    i wybuchy okresowe nie są. Wypowiedź = min. 3 takie ramki (~100 ms); jej początek (`onset`)
    liczy się od ciszy dłuższej niż 0,35 s. Pomyłka detektora oznacza najwyżej start lektora jak
    dotąd (od razu) — dlatego woli uznać muzykę za głos niż przegapić mowę."""

    FRAME = 512
    MIN_RUN = 3
    GAP = 0.35

    def __init__(self):
        self.lock = threading.Lock()
        self.buf = np.zeros(0, dtype=np.float32)
        self.floor = None
        self.run = 0
        self.fed = -1e9
        self.last_voice = -1e9
        self.onset = -1e9
        self._seg_start = -1e9
        self._confirmed = False

    def _periodic(self, frame):
        return _periodic_frame(frame)

    def feed(self, mono, now=None):
        if mono is None or mono.size == 0:
            return
        now = time.monotonic() if now is None else now
        with self.lock:
            buf = np.concatenate([self.buf, np.asarray(mono, dtype=np.float32)])
            n = buf.size // self.FRAME
            self.buf = buf[n * self.FRAME :]
            step = self.FRAME / float(SAMPLE_RATE)
            for i in range(n):
                frame = buf[i * self.FRAME : (i + 1) * self.FRAME]
                t = now - (n - 1 - i) * step - self.buf.size / float(SAMPLE_RATE)
                rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
                # tło: szybko w dół, powoli w górę (mowa nie podnosi go w trakcie wypowiedzi)
                self.floor = rms if self.floor is None or rms < self.floor else self.floor + 0.005 * (rms - self.floor)
                if rms > max(SPEECH_RMS, 1.25 * self.floor) and self._periodic(frame):
                    if t - self.last_voice > self.GAP:
                        self._seg_start, self._confirmed = t - step, False
                    self.run += 1
                    self.last_voice = t
                    if self.run >= self.MIN_RUN and not self._confirmed:
                        self._confirmed = True
                        self.onset = self._seg_start
                else:
                    self.run = 0
            self.fed = now

    def alive(self, now=None):
        now = time.monotonic() if now is None else now
        return now - self.fed < 1.0

    def speaking(self, now=None, hold=0.3):
        now = time.monotonic() if now is None else now
        return self._confirmed and now - self.last_voice < hold


class UtteranceCutter:
    """Dzieli strumień na wypowiedzi po ciszy; zwraca całą wypowiedź (z krótkim zapasem na początku)."""

    def __init__(self):
        self.preroll = []
        self.preroll_n = 0
        self.buf = []
        self.spoken = 0.0
        self.quiet = 0.0
        self.floor = None

    def _voice(self, mono):
        """Czy w kawałku jest głos: wyraźnie ponad tłem (ulica, tłum nigdy nie cichną) i okresowy
        jak głos — szum miasta to „cisza”, więc zdania NPC się rozdzielają; muzyka jest okresowa."""
        rms = float(np.sqrt(np.mean(np.square(mono))))
        self.floor = rms if self.floor is None or rms < self.floor else self.floor + 0.01 * (rms - self.floor)
        if rms < max(SPEECH_RMS, 1.1 * self.floor):
            return False
        return mono.size < 256 or _periodic_frame(mono[-512:], strict=True)

    def feed(self, mono):
        if mono is None or mono.size == 0:
            return None
        duration = mono.size / float(SAMPLE_RATE)
        loud = self._voice(mono)
        if not self.buf:
            if not loud:
                self.preroll.append(mono)
                self.preroll_n += mono.size
                while self.preroll and self.preroll_n - self.preroll[0].size >= int(SAMPLE_RATE * PREROLL_SEC):
                    self.preroll_n -= self.preroll.pop(0).size
                return None
            self.buf = self.preroll + [mono]
            self.preroll, self.preroll_n = [], 0
            self.spoken, self.quiet = duration, 0.0
            return None
        # w środku wypowiedzi bierzemy wszystko, także ciszę między słowami
        self.buf.append(mono)
        if loud:
            self.spoken += duration
            self.quiet = 0.0
        else:
            self.quiet += duration
        total = sum(part.size for part in self.buf) / float(SAMPLE_RATE)
        if self.quiet >= SILENCE_SEC or total >= MAX_SPEECH_SEC:
            audio = np.concatenate(self.buf)
            enough = self.spoken >= MIN_SPEECH_SEC
            self.buf, self.spoken, self.quiet = [], 0.0, 0.0
            return audio if enough else None
        return None


class LiveTranscriber:
    """Kolejka wypowiedzi: każda trafia do Parakeeta raz, w całości, i daje jedną kwestię."""

    def __init__(self, stt, on_line, on_error=None, on_ready=None):
        self.stt = stt
        self.on_line = on_line
        self.on_error = on_error
        self.on_ready = on_ready
        self.jobs = queue.Queue()
        self.thread = None
        self.ready = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, audio):
        # STT wolniejszy niż mowa: stare nagrania nie mogą się składać w RAM (każde to setki KB).
        # Sygnał stop (None) zostaje na końcu kolejki.
        if self.jobs.qsize() < 3:
            self.jobs.put(audio)
            return
        kept = []
        stop = False
        while True:
            try:
                old = self.jobs.get_nowait()
            except queue.Empty:
                break
            if old is None:
                stop = True
            else:
                kept.append(old)
        for old in kept[-2:]:
            self.jobs.put(old)
        self.jobs.put(audio)
        if stop:
            self.jobs.put(None)

    def stop(self):
        self.jobs.put(None)

    def _run(self):
        try:
            # MLX żyje we własnym wątku ParakeetSTT — tu tylko zlecamy zadania
            self.stt.ensure()
            self.stt.warmup()
            self.ready.set()
            if self.on_ready:
                self.on_ready()
            while True:
                audio = self.jobs.get()
                if audio is None:
                    return
                text = self.stt.transcribe(audio)
                if _speakable(text):
                    self.on_line(text, audio)
        except Exception as exc:
            self.ready.set()
            if self.on_error:
                self.on_error(str(exc))


# Interfejs gry (nie dialog): menu, koło broni, podpowiedzi przycisków, liczniki, komunikaty misji.
_UI_PROMPT = re.compile(
    r"(?i)\b(?:naciśnij|nacisnij|wciśnij|wcisnij|przytrzymaj|kliknij|użyj|uzyj|press|hold|tap|click|use)\b"
    r".{0,24}(?:\[[^\]]{1,6}\]|[△○□✕×⨯◯]|\b(?:[LR][123]|[LR]B|[LR]T|[ABXY]|E|F|Q|R|Esc|Enter|Spacj\w*|Space|Tab|Shift)\b)"
)
_UI_BUTTON = re.compile(r"(?:\[[A-Za-z0-9]{1,5}\]|[△○□✕◯])")
_UI_COUNTER = re.compile(r"\d+\s*/\s*\d+|\$\s?\d|\d\s?(?:%|zł|\$)|\b\d{1,2}:\d{2}\b|\b[xX]\s?\d+\b|\b\d+\s?[xX]\b")
_UI_WORDS = {polish_fold(w) for w in POLISH_UI} | {
    "map", "mapa", "settings", "options", "resume", "quit", "exit", "back", "select", "confirm", "cancel",
    "inventory", "weapons", "weapon", "ammo", "amunicja", "health", "zdrowie", "armor", "pancerz", "stats",
    "statystyki", "brief", "online", "story", "mode", "pause", "save", "load", "game", "gallery", "galeria",
    "help", "pomoc", "controls", "audio", "video", "display", "graphics", "camera", "kamera", "misja", "mission",
    "passed", "failed", "zaliczona", "nieudana", "wasted", "busted", "zginales", "zginąłeś", "aresztowany",
    "unarmed", "pieści", "piesci", "rzut", "radio", "telefon", "phone", "gps",
}
_UI_DIALOGUE_WORDS = None


# podpowiedzi przycisków GTA (warsztat, kamera, tryb foto) doklejane do dialogu: „Dobra. Jak nówka. ZOOM L
# Pierwsza osoba ElE RUSZAJ KAMERĄ” — wycinane razem ze skrótami przycisków obok
# wielowyrazowe podpowiedzi — w każdej wielkości liter; pojedyncze czasowniki tylko WIELKIMI
# („Wróć do domu, Franklin.” to dialog, „WRÓĆ” to przycisk)
_HUD_PROMPTS = re.compile(
    r"(?<!\w)(?:(?i:pierwsza osoba|trzecia osoba|ruszaj kamerą|obróć kamerę|zmień widok|ukryj menu|pokaż menu)"
    r"|ZOOM|PRZEGLĄDAJ|WRÓĆ|WYBIERZ|KUP|ZAMONTUJ|OBRÓĆ)(?!\w)"
)
_SHORT_PL = {
    "a", "i", "o", "u", "w", "z", "ze", "we", "na", "do", "to", "ta", "te", "ty", "tu", "ja", "mi", "ci", "go",
    "mu", "nie", "tak", "juz", "sie", "co", "czy", "no", "oj", "ej", "hej", "ale", "bo", "ze", "by", "od", "po",
    "za", "pod", "nad", "ma", "mam", "ich", "nas", "was", "on", "ona", "ono", "oni", "one", "tez", "raz", "dwa",
    "jak", "tam", "sam", "kto", "gdy", "bez", "dla", "lub", "ani", "ok", "hm", "ach", "och", "ech",
}


def strip_hud_prompts(text):
    raw = normalize_text(text)
    if not _HUD_PROMPTS.search(raw):
        return raw
    out = _HUD_PROMPTS.sub(" ", raw)
    # skróty przycisków i śmieci obok podpowiedzi („L”, „ElE”, „C”, „mil”, „!!”)
    words = [w for w in out.split() if polish_fold(w) in _SHORT_PL or not re.fullmatch(r"[^\s]{0,3}", w)]
    return normalize_text(" ".join(words))


def looks_like_game_ui(text):
    """Czy odczyt to element interfejsu gry, a nie kwestia postaci (wtedy lektor go pomija)."""
    global _UI_DIALOGUE_WORDS
    raw = normalize_text(text)
    if not raw:
        return True
    if _UI_PROMPT.search(raw) or _UI_BUTTON.search(raw):
        return True
    words = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż']+", raw)
    if not words:
        return True
    if _UI_COUNTER.search(raw) and len(words) <= 4:
        return True  # „Pistolet 12/120”, „$1 200”, „Zdrowie 100%”, „12:30”
    folded = [polish_fold(w) for w in words]
    if all(f in _UI_WORDS for f in folded if f):
        return True  # samo menu: „Mapa”, „Ustawienia”, „Wróć”, „Misja zaliczona”
    letters = [ch for ch in raw if ch.isalpha()]
    if len(words) <= 4 and len(letters) >= 4 and all(ch.isupper() for ch in letters):
        return True  # „MISJA ZALICZONA”, „WASTED”
    if len(words) <= 3 and not re.search(r"[.!?…,]", raw):
        # 1–3 słowa bez interpunkcji: nazwa broni/przedmiotu z koła wyboru („Karabin szturmowy”),
        # chyba że to krótka kwestia („Chodź tu”, „Let's go”)
        if _UI_DIALOGUE_WORDS is None:
            _UI_DIALOGUE_WORDS = (
                {polish_fold(w) for w in POLISH_DIALOGUE}
                | {polish_fold(w) for w in DIALOGUE_STARTERS}
                | {w.replace("'", "") for w in EN_COMMON}
            )
        if len(words) >= 2 and all(w[:1].isupper() for w in words):
            return True  # „Radio Los Santos”, „Combat Pistol” — nazwa, nie kwestia
        talk = [
            f for f, w in zip(folded, words)
            if f not in _UI_WORDS and (f in _UI_DIALOGUE_WORDS or w.lower().replace("'", "") in _UI_DIALOGUE_WORDS)
        ]
        if not talk:
            return True
    return False


DIALOGUE_STARTERS = {
    "ja", "ty", "on", "ona", "my", "wy", "to", "nie", "tak", "ale", "czy", "jak", "co",
    "gdzie", "kiedy", "dlaczego", "czemu", "moze", "musze", "chce", "prosze", "przepraszam",
    "dobrze", "swietnie", "chodz", "patrz", "sluchaj", "hej", "witaj", "dziekuje",
}


def looks_like_speaker_name(text):
    """Krótka etykieta imienia (Hogwarts: żółty nagłówek przed dialogiem)."""
    raw = normalize_text(text or "")
    if not raw or len(raw) > 42:
        return False
    if re.search(r"[!?…]", raw):
        return False
    letters = sum(ch.isalpha() for ch in raw)
    if letters < 2:
        return False
    words = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż']+", raw)
    # Imię + nazwisko (min. 2 słowa) — pojedyncze słowo zbyt ryzykowne bez koloru
    if len(words) < 2 or len(words) > 4:
        return False
    if any(polish_fold(w) in DIALOGUE_STARTERS for w in words):
        return False
    if not all(w[:1].isupper() for w in words):
        return False
    if raw.endswith((".", "!", "?")):
        return False
    return True


def region_is_yellow(rgb, box_px, margin=1):
    """Czy wycinek napisu jest żółty/złoty (Hogwarts speaker)."""
    if rgb is None or box_px is None or len(box_px) < 4:
        return False
    h, w = rgb.shape[:2]
    x, y, bw, bh = [int(v) for v in box_px[:4]]
    x0 = max(0, x + margin)
    y0 = max(0, y + margin)
    x1 = min(w, x + max(bw, 4) - margin)
    y1 = min(h, y + max(bh, 4) - margin)
    if x1 <= x0 + 2 or y1 <= y0 + 2:
        return False
    patch = rgb[y0:y1, x0:x1]
    if patch.size == 0:
        return False
    bright = patch[np.mean(patch, axis=2) > 70]
    sample = bright if len(bright) >= 8 else patch.reshape(-1, 3)
    r, g, b = [float(v) for v in np.mean(sample, axis=0)]
    return r >= 145 and g >= 115 and b <= 140 and (r + g) >= (2.1 * b + 40) and (r - b) >= 35


def strip_speaker_label(text):
    """Odetnij wiodące imię mówcy od dialogu (fallback gdy kolor nie wystarczy)."""
    raw = normalize_text(text)
    if not raw:
        return ""
    m = re.match(
        r"^([A-ZĄĆĘŁŃÓŚŹŻ][\w'ąćęłńóśźżĄĆĘŁŃÓŚŹŻ\-]*(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w'ąćęłńóśźżĄĆĘŁŃÓŚŹŻ\-]*){0,3})\s*[:\-–—]\s*(.+)$",
        raw,
    )
    if m and looks_like_speaker_name(m.group(1)) and len(m.group(2)) >= 6:
        return normalize_text(m.group(2))
    words = raw.split()
    if len(words) >= 3:
        for cut in (3, 2, 1):
            if len(words) <= cut:
                continue
            head = " ".join(words[:cut])
            tail = " ".join(words[cut:])
            if not looks_like_speaker_name(head):
                continue
            if looks_like_speaker_name(tail) or len(tail) < 8:
                continue
            # dialog zwykle ma małą literę w środku albo interpunkcję / słowo startowe
            tail_words = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż']+", tail)
            if not tail_words:
                continue
            if polish_fold(tail_words[0]) in DIALOGUE_STARTERS or any(ch.islower() for ch in tail) or re.search(r"[!?…,\.]", tail):
                return normalize_text(tail)
    return raw


class AppleVisionOcr:
    def __init__(self):
        self.boost = 2.4
        self.skip_yellow_speaker = False
        self.accurate_first = True
        self.languages = ["pl-PL"]

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

    @staticmethod
    def _candidate_score(text):
        raw = text or ""
        marks = sum(ch in PL_MARK for ch in raw)
        # karaj typowe pomyłki OCR bez polskich znaków
        folded = polish_fold(raw)
        penalty = 0
        if "lodz" in folded and "łódź" not in raw.lower() and "łodz" not in raw.lower():
            penalty += 4
        # „kasa”?” odczytane jako „ka' ' '” — taki kandydat przegrywa z czystym
        penalty += 15 * ocr_junk_count(raw)
        return marks * 12 + len(raw) - penalty

    def _pick_pl_candidate(self, observation):
        """Weź kandydata z największą liczbą ąęćłńóśźż (pierwszy bywa bez nich)."""
        try:
            cands = list(observation.topCandidates_(8) or [])
        except Exception:
            cands = []
        best_text = ""
        best_conf = 0.0
        best_score = -10**9
        for cand in cands:
            try:
                text = str(cand.string() or "").strip()
                conf = float(cand.confidence())
            except Exception:
                continue
            if not text:
                continue
            score = self._candidate_score(text) + conf
            if score > best_score:
                best_score = score
                best_text = text
                best_conf = conf
        if best_text:
            return best_text, best_conf
        # awaryjnie: stare API ocrmac
        try:
            text = str(observation.text() or "").strip()
            return text, float(getattr(observation, "confidence", lambda: 1.0)())
        except Exception:
            return "", 0.0

    def _vision_pl_rows(self, image, languages=None):
        """Vision accurate + pl-PL + language correction — fast gubi diakrytyki."""
        import Vision
        import objc
        from ocrmac.ocrmac import pil2buf

        langs = list(languages or self.languages or ["pl-PL"])
        if "pl-PL" not in langs:
            langs = ["pl-PL"] + langs
        rows = []
        with objc.autorelease_pool():
            req = Vision.VNRecognizeTextRequest.alloc().init()
            # 0 = accurate — tylko ten poziom oficjalnie wspiera pl-PL
            req.setRecognitionLevel_(0)
            try:
                # revision z pl-PL (3 na aktualnym macOS)
                supported = req.supportedRecognitionLanguagesAndReturnError_(None)[0] or []
                if "pl-PL" not in list(supported):
                    for rev in (3, 2):
                        try:
                            req.setRevision_(rev)
                            supported = req.supportedRecognitionLanguagesAndReturnError_(None)[0] or []
                            if "pl-PL" in list(supported):
                                break
                        except Exception:
                            continue
            except Exception:
                pass
            try:
                req.setUsesLanguageCorrection_(True)
            except Exception:
                pass
            try:
                available = list(req.supportedRecognitionLanguagesAndReturnError_(None)[0] or [])
                prefer = [lang for lang in langs if lang in available] or (
                    ["pl-PL"] if "pl-PL" in available else None
                )
                if prefer:
                    req.setRecognitionLanguages_(prefer)
            except Exception:
                pass
            handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(pil2buf(image), None)
            ret = handler.performRequests_error_([req], None)
            if isinstance(ret, tuple):
                ok, err = ret
            else:
                ok, err = bool(ret), None
            if not ok or err is not None:
                return rows
            for result in req.results() or []:
                text, conf = self._pick_pl_candidate(result)
                if not text or conf < 0.08:
                    continue
                bbox = result.boundingBox()
                x, y = bbox.origin.x, bbox.origin.y
                w, h = bbox.size.width, bbox.size.height
                rows.append((text, conf, [x, y, w, h]))
        return rows

    def _rows_to_items(self, rows, image):
        items = []
        bw, bh = image.size
        for row in rows:
            if not row:
                continue
            text = str(row[0]).strip()
            if not text:
                continue
            conf = float(row[1]) if len(row) > 1 else 1.0
            if conf < 0.08:
                continue
            box = row[2] if len(row) > 2 else None
            if box and len(box) >= 4 and max(abs(float(v)) for v in box[:4]) <= 1.5:
                x, y, w, h = [float(v) for v in box[:4]]
                px = (int(x * bw), int((1.0 - y - h) * bh), int(w * bw), int(h * bh))
                y_sort = 1.0 - y
            elif box and len(box) >= 4:
                px = tuple(int(v) for v in box[:4])
                y_sort = float(px[1]) / max(bh, 1)
            else:
                px = None
                y_sort = 0.0
            x_sort = float(px[0]) if px else 0.0
            items.append((round(y_sort, 3), x_sort, text, px, conf))
        items.sort()
        return items

    def _run_items(self, image, framework="vision", recognition_level="accurate", languages=None):
        # Nigdy fast dla polskiego — Vision wtedy czyta „Mysle” zamiast „Myślę”.
        if framework == "vision":
            try:
                rows = self._vision_pl_rows(image, languages=languages)
                if rows:
                    return self._rows_to_items(rows, image)
            except Exception:
                pass
        from ocrmac import ocrmac

        kwargs = {"framework": framework, "detail": True}
        if framework == "vision":
            kwargs["recognition_level"] = "accurate"
            kwargs["confidence_threshold"] = 0.08
            kwargs["language_preference"] = list(languages or self.languages or ["pl-PL"])
        elif languages:
            kwargs["language_preference"] = languages
        try:
            ocr = ocrmac.OCR(image, **kwargs)
            try:
                rows = ocr.recognize(px=True)
            except TypeError:
                rows = ocr.recognize()
        except Exception:
            return []
        return self._rows_to_items(rows, image)

    def _join_items(self, items, rgb=None):
        kept = []
        for _y, _x, text, box, _conf in items:
            if self.skip_yellow_speaker and rgb is not None and region_is_yellow(rgb, box):
                continue
            if self.skip_yellow_speaker and looks_like_speaker_name(text) and kept:
                continue
            if self.skip_yellow_speaker and looks_like_speaker_name(text) and not kept:
                continue
            kept.append(text)
        if not kept and items:
            texts = [t for _y, _x, t, _b, _c in items]
            if len(texts) >= 2 and looks_like_speaker_name(texts[0]):
                kept = texts[1:]
            else:
                kept = texts
        return normalize_text(" ".join(kept))

    def _run(self, image, framework="vision", recognition_level="accurate", languages=None, rgb=None):
        items = self._run_items(image, framework, "accurate", languages)
        if self.skip_yellow_speaker:
            return self._join_items(items, rgb=rgb)
        return normalize_text(" ".join(item[2] for item in items))

    def _score(self, text):
        raw = normalize_text(text)
        if not usable_ocr(raw):
            return -1
        fixed = repair_polish_ocr(raw)
        score = len(fixed)
        score += 20 * sum(ch in PL_MARK for ch in raw)
        score += 14 * sum(ch in PL_MARK for ch in fixed if ch not in raw)
        score += 10 * sum(1 for ch in raw if ch in "!?…")
        score += 4 * raw.count(".")
        score += 2 * sum(1 for token in fixed.lower().split() if polish_fold(token) in _POLISH_BY_FOLD)
        score -= 4 * len(re.findall(r"[A-Za-z][0-9]|[0-9][A-Za-z]", raw))
        score -= 2 * len(re.findall(r"[0-9]", raw))
        score -= 15 * ocr_junk_count(raw)
        # kara za brak diakrytyków przy długim polskim tekście
        if len(raw) >= 12 and sum(ch in PL_MARK for ch in raw + fixed) == 0 and looks_polish(fixed):
            score -= 18
        if self.skip_yellow_speaker and looks_like_speaker_name(raw):
            score -= 30
        return score

    @staticmethod
    def _local_min_max(lum, r):
        """Minimum i maksimum jasności w kwadracie (2r+1)² wokół każdego piksela."""
        h, w = lum.shape
        pad = np.pad(lum, r, mode="edge")
        lo_rows = pad[:, 0:w].copy()
        hi_rows = lo_rows.copy()
        for dx in range(1, 2 * r + 1):
            np.minimum(lo_rows, pad[:, dx : dx + w], out=lo_rows)
            np.maximum(hi_rows, pad[:, dx : dx + w], out=hi_rows)
        lo = lo_rows[0:h].copy()
        hi = hi_rows[0:h].copy()
        for dy in range(1, 2 * r + 1):
            np.minimum(lo, lo_rows[dy : dy + h], out=lo)
            np.maximum(hi, hi_rows[dy : dy + h], out=hi)
        return lo, hi

    @staticmethod
    def subtitle_mask(frame):
        """Białe/szare litery przy czarnej obwódce lub tle → czarny tekst na białym tle.
        Kolorowe litery i jasne tło bez ciemnego sąsiedztwa nie trafiają do OCR."""
        rgb = frame[:, :, ::-1].astype(np.float32) if frame.shape[-1] == 3 else frame.astype(np.float32)
        lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
        spread = rgb.max(axis=2) - rgb.min(axis=2)
        text = (lum > 175) & (spread < 35)
        # napis ma ciemną obwódkę albo ciemne tło (Netflix, YouTube) — jasne plamy sceny bez niej odpadają
        # (zasięg ~6 px w każdą stronę, liczony osobno w poziomie i w pionie — gruba litera zostaje cała)
        r = max(4, text.shape[0] // 30)
        h, w = text.shape
        dark = np.pad(lum < 80, ((0, 0), (r, r)))
        rows = np.zeros((h, w), dtype=bool)
        for dx in range(-r, r + 1):
            rows |= dark[:, r + dx : r + dx + w]
        rows = np.pad(rows, ((r, r), (0, 0)))
        near_dark = np.zeros((h, w), dtype=bool)
        for dy in range(-r, r + 1):
            near_dark |= rows[r + dy : r + dy + h, :]
        strict = text & near_dark
        share = float(strict.mean())
        # napis to kilka–kilkanaście % pikseli; więcej = jasna scena/tło strony, nie napis
        if 0.003 <= share <= 0.35:
            text = strict
        else:
            # RDR2: szare litery (~165) na ciemnym pasku, a na śniegu miękki cień nie schodzi poniżej 80 —
            # litera to lokalny szczyt jasności z wyraźnie ciemniejszym sąsiedztwem, jasne tło samo odpada
            lo, hi = AppleVisionOcr._local_min_max(lum, r)
            text = (lum > 130) & (spread < 35) & (lum >= hi - 18) & (lo < lum - 95)
            share = float(text.mean())
            # krótka kwestia („Zabrali ją dokądś.”) to ~0,2 % szerokiego paska — liczy się ilość liter, nie udział
            if int(text.sum()) < 150 or share > 0.35:
                return None
        mask = np.where(text, 0, 255).astype(np.uint8)
        image = Image.fromarray(mask).convert("RGB")
        width, height = image.size
        if height < 120:
            scale = 120 / max(height, 1)
            image = image.resize((max(8, int(width * scale)), 120), Image.Resampling.LANCZOS)
        return image

    def _frame_image(self, frame, size):
        rgb = frame[:, :, ::-1] if frame.shape[-1] == 3 else frame
        image = Image.fromarray(np.ascontiguousarray(rgb)).convert("RGB")
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        return image

    def _text_on_ink(self, image, ink):
        """Zostaw tylko odczyt leżący na białych literach napisu. Tablica i HUD odpadają."""
        try:
            items = self._run_items(image, "vision", "accurate", self.languages)
        except Exception:
            return ""
        height, width = ink.shape
        kept = []
        for _y, _x, text, box, _conf in items:
            if box is None:
                continue  # bez położenia nie możemy sprawdzić wyglądu liter
            x, y, w, h = box
            crop = ink[max(0, y):min(height, y + h), max(0, x):min(width, x + w)]
            # Jasna tablica z ciemnymi literami tworzy niemal pełną plamę,
            # a białe litery napisu — cienkie, oddzielone kreski.
            if crop.size == 0 or not 0.025 <= float(crop.mean()) <= 0.55:
                continue
            if self.skip_yellow_speaker and looks_like_speaker_name(text):
                continue
            kept.append(text)
        return normalize_text(" ".join(kept))

    def read(self, frame):
        if frame is None or frame.size == 0:
            return ""
        mask = self.subtitle_mask(frame)
        if mask is None:
            return ""
        # Maska tylko wykrywa napis. Sam obraz z maski Vision czyta dobrze na Netflixie
        # (grube białe litery na czarnym), a w grze — biały tekst z obwódką na jasnej
        # scenie — zamienia w plamy („Wpadłem” → „Wpadiem”). Kolorowy kadr tego paska
        # czyta te same litery. Bez maski wcale nie czytamy: inaczej lecą tablice i HUD.
        ink = np.asarray(mask)[:, :, 0] < 128
        color = self._frame_image(frame, mask.size)
        if IS_WIN:
            primary, secondary = mask, color
        else:
            primary, secondary = color, mask
        raw = self._text_on_ink(primary, ink)
        if self._score(raw) < 14:
            other = self._text_on_ink(secondary, ink)
            if self._score(other) > self._score(raw):
                raw = other
        best = repair_polish_ocr(raw)
        return best if usable_ocr(best) else ""


class WindowsOcr(AppleVisionOcr):
    """Windows: ten sam wybór odczytu i poprawki PL co przy Vision, rozpoznawanie — Windows.Media.Ocr."""

    def __init__(self):
        super().__init__()
        self.backend = winplat.WindowsOcrBackend()

    def _run_items(self, image, framework="vision", recognition_level="accurate", languages=None):
        if self.backend.error:
            raise RuntimeError(self.backend.error)
        rows = self.backend.recognize(image, languages or self.languages)
        return self._rows_to_items(rows, image)



class Engine:
    def __init__(self, emit):
        self.emit = emit
        self.cfg = load_config()
        self.region = tuple(self.cfg["region"]) if self.cfg.get("region") else None
        self.mode = normalize_mode(self.cfg.get("mode", "auto"))
        self.device = self.cfg.get("device") or PS_REMOTE
        self.overlay = False
        # skan zawsze według wybranej gry (bez ręcznego suwaka)
        self.auto_interval = True
        self.interval = DEFAULT_INTERVAL
        # głośność lektora 0–100 %
        self.lektor_volume = max(0, min(100, int(self.cfg.get("lektorVolume", 80))))
        self.running = False
        self.last_key = ""
        self.last_subtitle = ""
        self.subtitle_until = 0.0
        self.speaking_text = ""
        self.speaking_full = ""
        self.last_full = ""
        self._ocr_candidate = ""
        self._ocr_candidate_n = 0
        self._spoken_folds = {}
        self._heard_seen = {}
        self._heard_offered_at = 0.0
        self.confirm_frames = OCR_CONFIRM_FRAMES
        self.speak_cooldown = 5.5
        self.no_barge_in = True
        self.pending_lock = threading.Lock()
        # kolejka kwestii do przeczytania (po kolei); self.pending = następna z nich
        self._queue = []
        self.has_pending = threading.Event()
        self._tts_interrupt = threading.Event()
        # rośnie przy każdym Stop/flush — przerywa czytanie wielozdaniowej kwestii
        self._speech_gen = 0
        self.game = normalize_game(self.cfg.get("game", "gta6"))
        if not self.cfg.get("gta6Added"):
            # GTA VI to główny cel aplikacji — jednorazowo ustaw ją jako wybraną grę
            self.game = "gta6"
        self.game_regions = dict(self.cfg.get("games") or {})
        self.lock_region = bool(self.cfg.get("lockRegion", False))
        self.show_region = bool(self.cfg.get("showRegion", False))
        self.dock_corner = str(self.cfg.get("dockCorner") or "tr")
        self.collapsed = bool(self.cfg.get("collapsed", False))
        # autostart: lektor rusza sam, gdy pojawi się okno PS Remote Play
        self.auto_start = bool(self.cfg.get("autoStart", True))
        # źródło: auto (PS Remote Play, a bez niego Chrome z Netflixem/YouTube), ps, chrome
        source = str(self.cfg.get("source") or "auto").lower()
        if source == "youtube":
            source = "chrome"  # osobne źródło YouTube zostało usunięte — Chrome czyta każdą stronę
        self.source = source if source in SOURCES else "auto"
        self.source_info = None
        self._last_ocr_logged = ""
        self._controls_logged = 0.0
        # ściszanie gry, gdy mówi lektor (jak prawdziwy lektor w filmie)
        # o ile ściszyć grę, gdy mówi lektor: 0 % = wcale (dźwięku gry nie przejmujemy), 100 % = cisza
        self.duck_amount = max(0, min(100, int(self.cfg.get("duckAmount", 70))))
        self._tap = None
        self._win_ducker = None
        self._duck_timer = None
        self._auto_started = False
        self._user_stopped = False
        if self.dock_corner not in ("tl", "tr", "bl", "br"):
            self.dock_corner = "tr"
        self._last_win_sync = 0.0
        self.ps_window = None
        self._region_key_cur = None
        self.ocr = WindowsOcr() if IS_WIN else AppleVisionOcr()
        self._ocr_lang_warned = False
        self.lektor = MaleLektor()
        voice = str(self.cfg.get("lektorVoice") or DEFAULT_SUPERTONIC_VOICE).strip().upper()
        if voice in SUPERTONIC_VOICES:
            self.lektor.voice = voice
        self._cloud_error_shown = ""
        self.lektor.on_cloud_error = self._on_cloud_error
        # silnik lektora: supertonic (lokalnie, CPU) albo voicestudio (voicepack, GPU)
        packs = voicepacks() if voicestudio_python() else []
        self.omni_pack = str(self.cfg.get("omniPack") or (packs[0] if packs else ""))
        # jest voicepack z VoiceStudio — domyślnie on (naturalniejszy); wybór w „Więcej” → Głos
        default = "voicestudio" if packs else "supertonic"
        # wybór z „Więcej” obowiązuje; stary zapis bez wyboru użytkownika (sprzed voicepacka) — nie
        self.lektor_engine = (
            self.cfg.get("lektorEngine")
            if self.cfg.get("lektorEngineChosen") and self.cfg.get("lektorEngine") in ("supertonic", "voicestudio")
            else default
        )
        self._engine_chosen = bool(self.cfg.get("lektorEngineChosen"))
        self._sync_voice()
        self.translator = ArgosTranslator()
        self.stt = ParakeetSTT()
        self.prosody = ProsodyMeter()
        # czy postać teraz mówi (dźwięk gry) — lektor wchodzi chwilę po niej, jak w filmie
        self.voice = VoiceActivity()
        # „open jev”: mały model w tle ocenia napisy (dialog czy śmieć z ekranu, czy niesie informację)
        self.brain = LektorBrain(log=log_timing) if LektorBrain is not None and brain_supported() else None
        self._recurring = RecurringFragments()
        self._junk_logged = ""
        # ostatnie przeczytane kwestie (czas, tekst) — do rozpoznania napisu, który tylko urósł
        self._spoken_recent = deque(maxlen=8)
        # kiedy ostatnio gra pokazała napis — wtedy dialogi bierzemy tylko z napisów
        self._last_subtitle_seen = 0.0
        self._heard_arousal = {}
        self._pl_subs_at = None
        # tempo napisów: ile znaków na sekundę gra pokazuje (z czasu między kolejnymi napisami)
        self._sub_cps = None
        self._sub_cps_samples = []
        self._sub_prev = None
        # o ile sekund lektor spóźnia się za napisami — skracanie tylko przy prawdziwym spóźnieniu
        self._lag = 0.0
        self._speaking_parts = []
        self._prefetch_parts = None
        # kwestie przygotowane zawczasu: src → (tekst, fragmenty, przejęcie, wav)
        self._ready = {}
        self._ready_lock = threading.Lock()
        # synteza „na zapas” od pierwszego odczytu napisu, zanim OCR go potwierdzi
        self._spec_item = None
        self._spec_event = threading.Event()
        self._spec_busy = None
        self._spec_key, self._spec_rank, self._spec_redo = "", -1, 0
        self._spec_done = threading.Condition()
        self._seen_at = {}
        self.line_q = queue.Queue()
        self.transcriber = None
        self.devices = [PS_REMOTE] + [name for _i, name in list_input_devices()]
        self.apply_game(self.game, persist=False, announce=False, reset_lock=False)
        threading.Thread(target=self._tts_loop, daemon=True).start()
        threading.Thread(target=self._spec_loop, daemon=True).start()
        threading.Thread(target=self._warmup_voice, daemon=True).start()
        threading.Thread(target=self._watch_remote_play, daemon=True).start()

    def _watch_remote_play(self):
        """Pilnuje okna gry (PS Remote Play / Chrome): autostart, autostop i podpis okna w UI."""
        missing = 0.0
        step = 2.0
        while True:
            time.sleep(step)
            try:
                info = find_source_window(self.source, GAME_PROFILES[self.game].get("prefer", "ps"))
            except Exception:
                continue
            win = info[:4] if info else None
            kind = info[5] if info else None
            # Windows: ruch i zmiana rozmiaru okna odświeżają jego podpis w widżecie
            moved = IS_WIN and win != self.ps_window
            if moved or (win is None) != (self.ps_window is None) or kind != self._source_kind():
                self.ps_window = win
                self.source_info = info
                self.emit({"event": "state", **self.snapshot()})
            if win is None:
                missing += step
                # po zamknięciu Remote Play ręczny Stop przestaje obowiązywać
                if missing >= 10.0:
                    self._user_stopped = False
                    if self.running and self._auto_started:
                        self.stop()
                        self.emit({"event": "status", "text": "Gra zamknięta — lektor czeka."})
                continue
            missing = 0.0
            if self.auto_start and not self.running and not self._user_stopped:
                self.emit({"event": "status", "text": f"Wykryłem {self._source_label()} — startuję lektora."})
                self.start(auto=True)

    @property
    def duck(self):
        return self.duck_amount > 0

    @property
    def duck_level(self):
        return 1.0 - self.duck_amount / 100.0

    def _set_game_gain(self, value):
        if IS_WIN:
            # Windows: głośność aplikacji źródła (Chrome/Edge/PS Remote Play) w mikserze systemu
            kind = self._source_kind()
            if kind and (self.duck or value >= 0.999):
                if self._win_ducker is None:
                    self._win_ducker = winplat.WinDucker()
                self._win_ducker.set_gain(kind, value)
            return
        tap = self._tap
        if tap is not None:
            tap.set_gain(value)

    def _duck_on(self):
        if self._duck_timer is not None:
            self._duck_timer.cancel()
            self._duck_timer = None
        if self.duck:
            self._set_game_gain(self.duck_level)

    def _duck_release(self):
        # chwila zapasu: kolejna kwestia zaraz po poprzedniej nie „pompuje” głośności gry
        if self._duck_timer is not None:
            self._duck_timer.cancel()
        self._duck_timer = threading.Timer(0.35, lambda: self._set_game_gain(1.0))
        self._duck_timer.daemon = True
        self._duck_timer.start()

    def _source_kind(self):
        return self.source_info[5] if self.source_info else None

    def _region_key(self):
        """Pod jakim kluczem zapisany jest ręczny pasek: gra (PS5) albo przeglądarka — Chrome ma własny
        pasek, niezależny od gry wybranej na liście."""
        return "chrome" if self._source_kind() == "chrome" else self.game

    def _source_label(self):
        if not self.source_info:
            return "grę"
        if self.source_info[5] == "chrome":
            title = self.source_info[6] or ""
            name = next((v.title() for v in VIDEO_TITLES if v in title.lower()), "")
            return f"Chrome ({name})" if name else "Chrome"
        return "PS Remote Play"

    def _band_profile(self):
        profile = dict(GAME_PROFILES[self.game])
        if self._source_kind() == "chrome":
            profile.update(CHROME_BAND)
        return profile

    def _warmup_voice(self):
        self.emit({"event": "status", "text": "Ładuję lektora…"})
        try:
            self.lektor.ensure()
            self.emit({"event": "status", "text": f"Lektor gotowy (Supertonic {self.lektor.voice}, {self.lektor.device_label})."})
        except Exception as exc:
            self.emit({"event": "status", "text": f"Lektor nie wstaje: {exc}"})
        if self.brain is not None:
            self.brain.start()  # po lektorze: pierwszy głos nie czeka na wczytanie modelu decyzji

    def snapshot(self):
        return {
            "region": list(self.region) if self.region else None,
            "mode": self.mode,
            "device": self.device,
            "overlay": self.overlay,
            "interval": self.interval,
            "autoInterval": self.auto_interval,
            "lektorVolume": self.lektor_volume,
            "game": self.game,
            "games": [{"id": key, "label": item["label"]} for key, item in GAME_PROFILES.items()],
            "gameHint": GAME_PROFILES[self.game]["hint"],
            "devices": getattr(self, "devices", [PS_REMOTE]),
            "running": self.running,
            "psWindow": list(self.ps_window) if self.ps_window else None,
            "lockRegion": self.lock_region,
            "showRegion": self.show_region,
            "dockCorner": self.dock_corner,
            "collapsed": self.collapsed,
            "autoStart": self.auto_start,
            "duckAmount": self.duck_amount,
            "source": self.source,
            "sourceKind": self._source_kind(),
            "sourceLabel": self._source_label() if self.source_info else None,
            "lektorEngine": getattr(self, "lektor_engine", "supertonic"),
            "omniPack": getattr(self, "omni_pack", ""),
            "omniPacks": voicepacks() if voicestudio_python() else [],
        }

    def _sync_voice(self):
        """Ustaw silnik lektora według ustawień; lokalny model VoiceStudio startuje tylko, gdy wybrany."""
        engine = self.lektor_engine
        cur = self.lektor.cloud
        if engine == "voicestudio" and self.omni_pack:
            if isinstance(cur, OmniVoiceLocal) and cur.pack == self.omni_pack:
                return
            backend = OmniVoiceLocal(self.omni_pack)
            self.emit({"event": "status", "text": f"Ładuję lektora VoiceStudio ({self.omni_pack})…"})

            def ready(b):
                if self.lektor.cloud is b:
                    self._on_cloud_error(b.error if b.error else f"Lektor: VoiceStudio — {b.pack}.")

            backend.on_ready = ready
            self.lektor.set_backend(backend)
        else:
            self.lektor.set_backend(None)

    def _on_cloud_error(self, text):
        if text != self._cloud_error_shown:
            self._cloud_error_shown = text
            self.emit({"event": "status", "text": text})

    def persist(self):
        save_config(
            {
                "region": list(self.region) if self.region else None,
                "overlay": self.overlay,
                "interval": self.interval,
                "autoInterval": self.auto_interval,
                "lektorVolume": self.lektor_volume,
                "mode": self.mode,
                "device": self.device,
                "game": self.game,
                "games": self.game_regions,
                "lockRegion": self.lock_region,
                "showRegion": self.show_region,
                "dockCorner": self.dock_corner,
                "collapsed": self.collapsed,
                "autoStart": self.auto_start,
                "duckAmount": self.duck_amount,
                "source": self.source,
                "gta6Added": True,
                "lektorVoice": self.lektor.voice,
                "lektorEngine": self.lektor_engine,
                "lektorEngineChosen": getattr(self, "_engine_chosen", False),
                "omniPack": self.omni_pack,
            }
        )

    def save_settings(self):
        # Zapisz = utrwal aktualny pasek jako ręczny (nie nadpisuj go automatem okna)
        if self.region:
            self.lock_region = True
            self.game_regions[self._region_key()] = {"region": [int(v) for v in self.region]}
        self.persist()
        if self.region:
            x, y, w, h = [int(v) for v in self.region]
            self.emit({"event": "status", "text": f"Zapisano pasek skanu {w}×{h} (zablokowany)."})
        else:
            self.emit({"event": "status", "text": "Ustawienia zapisane."})
        self.emit({"event": "state", **self.snapshot()})

    def configure(self, data):
        if "mode" in data:
            self.mode = normalize_mode(data["mode"])
        if "device" in data:
            self.device = data["device"]
        if "overlay" in data:
            self.overlay = bool(data["overlay"])
        if "lektorVolume" in data:
            self.lektor_volume = max(0, min(100, int(data["lektorVolume"])))
        if data.get("lektorEngine") in ("supertonic", "voicestudio"):
            self.lektor_engine = data["lektorEngine"]
            self._engine_chosen = True
            self._cloud_error_shown = ""
            if self.lektor_engine == "voicestudio" and not self.omni_pack and voicepacks():
                self.omni_pack = voicepacks()[0]
            self._sync_voice()
            if self.lektor_engine == "supertonic":
                self.emit({"event": "status", "text": "Lektor: Supertonic."})
        if "omniPack" in data and data["omniPack"] in voicepacks():
            self.omni_pack = data["omniPack"]
            self._sync_voice()
        if "game" in data:
            self.apply_game(data["game"], persist=False, announce=True, reset_lock=True)
        if "showRegion" in data:
            self.show_region = bool(data["showRegion"])
        if "dockCorner" in data and data["dockCorner"] in ("tl", "tr", "bl", "br"):
            self.dock_corner = data["dockCorner"]
        if "lockRegion" in data:
            self.lock_region = bool(data["lockRegion"])
        if "collapsed" in data:
            self.collapsed = bool(data["collapsed"])
        if "autoStart" in data:
            self.auto_start = bool(data["autoStart"])
        if "duckAmount" in data:
            was = self.duck
            self.duck_amount = max(0, min(100, int(data["duckAmount"])))
            if self.duck != was and self.running:
                # włączenie/wyłączenie ściszania = inny rodzaj przejęcia dźwięku — podepnij od nowa
                auto = self._auto_started
                self.stop()
                threading.Timer(0.6, lambda: self.start(auto=auto)).start()
        if data.get("source") in SOURCES and data["source"] != self.source:
            self.source = data["source"]
            self.source_info = find_source_window(self.source, GAME_PROFILES[self.game].get("prefer", "ps"))
            self.ps_window = self.source_info[:4] if self.source_info else None
            self.lock_region = False
            self._sync_remote_band(force=True)
            # inne okno/dźwięk — podepnij od nowa
            if self.running:
                auto = self._auto_started
                self.stop()
                threading.Timer(0.6, lambda: self.start(auto=auto)).start()
        self.persist()
        self.emit({"event": "state", **self.snapshot()})

    def apply_game(self, game, persist=True, announce=True, reset_lock=True):
        # inna gra = może mieć inne napisy; polskie wykryjemy od nowa
        self._pl_subs_at = None
        self.game = normalize_game(game)
        profile = GAME_PROFILES[self.game]
        if self.auto_interval:
            self.interval = float(profile["interval"])
        self.ocr.boost = float(profile["boost"])
        self.ocr.skip_yellow_speaker = bool(profile.get("skip_yellow_speaker"))
        self.ocr.accurate_first = bool(profile.get("ocr_accurate_first"))
        langs = profile.get("ocr_langs") or ["pl-PL"]
        self.ocr.languages = list(langs)
        self.lektor.tts_scale = float(profile["tts"])
        self.confirm_frames = max(1, int(profile.get("confirm_frames", OCR_CONFIRM_FRAMES)))
        self.speak_cooldown = float(profile.get("speak_cooldown", 5.5))
        self.no_barge_in = bool(profile.get("no_barge_in", True))
        # lektor nie wyrabia → zaległe kwestie przepadają (wszystkie gry i Chrome)
        self.catch_up = bool(profile.get("catch_up", True))
        self.catch_up_keep = max(1, int(profile.get("catch_up_keep", 2)))
        if reset_lock:
            self.lock_region = False
            saved = self.game_regions.get(self._region_key()) or {}
            if saved.get("region"):
                self.region = tuple(saved["region"])
                self.lock_region = True
        if not self.lock_region:
            self._sync_remote_band(force=True)
        if persist:
            self.persist()
        if announce:
            if self.lock_region and self.region:
                self.emit({"event": "status", "text": f"{profile['label']}: mam zapisany ręczny pasek."})
            elif self.ps_window:
                _l, _t, width, height = self.ps_window
                self.emit({"event": "status", "text": f"{profile['label']}: mam {self._source_label()} {width}×{height}."})
            else:
                self.emit({"event": "status", "text": f"{profile['label']}: czekam na grę (PS Remote Play albo Netflix/YouTube w Chrome)."})

    def _region_overlap(self, region, win):
        if not region or not win:
            return 0.0
        rx, ry, rw, rh = [int(v) for v in region]
        wx, wy, ww, wh = [int(v) for v in win]
        if rw <= 0 or rh <= 0:
            return 0.0
        ix1, iy1 = max(rx, wx), max(ry, wy)
        ix2, iy2 = min(rx + rw, wx + ww), min(ry + rh, wy + wh)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        return (iw * ih) / float(rw * rh)

    def _sync_remote_band(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_win_sync < 1.2:
            return self.ps_window is not None or self.region is not None
        self._last_win_sync = now
        info = find_source_window(self.source, GAME_PROFILES[self.game].get("prefer", "ps"))
        self.source_info = info
        win = info[:4] if info else None
        self.ps_window = win
        key = self._region_key() if info else self._region_key_cur
        if key != self._region_key_cur:
            # inne źródło (PS5 ↔ Chrome) — jego własny zapisany pasek albo automat
            first = self._region_key_cur is None
            self._region_key_cur = key
            saved = self.game_regions.get(key) or {}
            if saved.get("region"):
                self.region = tuple(saved["region"])
                self.lock_region = True
            elif not first or self._source_kind() == "chrome":
                self.lock_region = False
        if self.lock_region and self.region:
            # Ręcznie zapisany pasek zostaje — nie kasuj go przez overlap/automatu.
            if win is None or self._region_overlap(self.region, win) >= 0.12:
                return True
            # okno się przesunęło: dociągnij pasek do dolnego pasa, zachowaj lock
            band = window_subtitle_band(win, self._band_profile())
            # jeśli stary pasek był wyżej/niżej, zachowaj względną wysokość w oknie
            rx, ry, rw, rh = [int(v) for v in self.region]
            _wx, wy, _ww, wh = [int(v) for v in win]
            rel = (ry - wy) / max(wh, 1)
            if 0.35 <= rel <= 0.95:
                new_top = int(wy + rel * wh)
                self.region = (band[0], max(wy, min(new_top, wy + wh - rh)), band[2], rh)
            else:
                self.region = band
            self.game_regions[self._region_key()] = {"region": list(self.region)}
            self.emit({"event": "state", **self.snapshot()})
            return True
        if win is None:
            return self.region is not None
        band = window_subtitle_band(win, self._band_profile())
        if band != self.region:
            self.region = band
            self.emit({"event": "state", **self.snapshot()})
        return True

    def pick(self):
        self.stop()
        if self.mode == "audio":
            self.mode = "ocr"
        self.emit({"event": "status", "text": "Przeciągnij pasek napisów na grze. Esc anuluje."})
        region = pick_region_native()
        self.region = region
        self.lock_region = region is not None
        if region:
            self.game_regions[self._region_key()] = {"region": list(region)}
        self.persist()
        if region is None:
            self.emit({"event": "status", "text": "Nie zaznaczono obszaru."})
            self.emit({"event": "state", **self.snapshot()})
            return
        self.emit({"event": "state", **self.snapshot()})
        self._preview_and_ocr(speak=False)

    def test(self):
        if not self._sync_remote_band(force=True) and self.region is None:
            self.emit({"event": "status", "text": "Nie widzę gry. Odpal PS Remote Play albo Netflix/YouTube w Chrome."})
            return
        self.persist()
        self.emit({"event": "status", "text": "Robię test napisów…"})
        self._preview_and_ocr(speak=True)

    def start(self, auto=False):
        if self.running:
            return
        self._user_stopped = False
        self._auto_started = auto
        self.running = True
        self.last_key = ""
        self.last_subtitle = ""
        self.subtitle_until = 0.0
        self.speaking_text = ""
        self.speaking_full = ""
        self.last_full = ""
        self._ocr_candidate = ""
        self._ocr_candidate_n = 0
        self._spoken_folds = {}
        self._tts_interrupt.clear()
        self._flush_line_q()
        self.emit({"event": "running", "on": True})
        # bez zgody na nagrywanie ekranu napisy nie działają (np. po aktualizacji aplikacji
        # macOS traktuje ją jak nową i zgodę trzeba dać jeszcze raz) — powiedz to wprost
        if self.mode != "audio" and not screen_access_ok():
            self.running = False
            self.emit({"event": "running", "on": False})
            self.emit(
                {
                    "event": "status",
                    "text": "Brak zgody na nagrywanie ekranu — nie widzę napisów. Ustawienia → Prywatność → "
                    "Nagrywanie ekranu: włącz LiveDub (usuń stary wpis i dodaj ponownie), potem uruchom LiveDub od nowa.",
                }
            )
            # autostart nie ponawia co 2 s, a Ustawienia otwieramy raz na uruchomienie
            self._user_stopped = True
            if not getattr(self, "_screen_warned", False):
                self._screen_warned = True
                open_screen_settings()
            return
        if IS_WIN:
            # Windows: na razie tylko napisy (tłumaczenie dźwięku w kolejnej wersji)
            if not self._sync_remote_band(force=True) and not self.lock_region:
                self.running = False
                self.emit({"event": "running", "on": False})
                self.emit({"event": "status", "text": "Nie widzę gry. Odpal PS Remote Play albo Netflix/YouTube w Chrome."})
                return
            note = " (dźwięk EN→PL na Windowsie będzie w kolejnej wersji)" if self.mode == "audio" else ""
            self.emit({"event": "status", "text": f"Napisy z {self._source_label()}{note}."})
            target = self._ocr_scan_thread
        elif self.mode == "audio":
            self.emit({"event": "status", "text": f"Podpinam dźwięk: {self._source_label()}…"})
            target = self._audio_loop
        elif self.mode == "ocr":
            if not self._sync_remote_band(force=True) and not self.lock_region:
                self.running = False
                self.emit({"event": "running", "on": False})
                self.emit({"event": "status", "text": "Nie widzę gry. Odpal PS Remote Play albo Netflix/YouTube w Chrome."})
                return
            self.emit({"event": "status", "text": f"Tylko napisy — {self._source_label()}."})
            target = self._ocr_only_loop
        elif not self._sync_remote_band(force=True) and not self.lock_region:
            self.emit({"event": "status", "text": "Nie widzę okna gry — czytam z dźwięku."})
            target = self._audio_loop
        else:
            self.emit({"event": "status", "text": f"Napisy z {self._source_label()}, dźwięk EN→PL w tle."})
            target = self._hybrid_loop
        threading.Thread(target=target, daemon=True).start()

    def stop(self, user=False):
        if user:
            # ręczny Stop: nie odpalaj się sam, dopóki Remote Play nie zniknie
            self._user_stopped = True
            self._auto_started = False
        self.running = False
        self._flush_line_q()
        self.speaking_text = ""
        if self.transcriber is not None:
            self.transcriber.stop()
            self.transcriber = None
        self.lektor.stop()
        if self._win_ducker is not None:
            self._win_ducker.set_gain(self._source_kind() or "chrome", 1.0)
        self.emit({"event": "running", "on": False})
        self.emit({"event": "status", "text": "Zatrzymane."})

    def _preview_and_ocr(self, speak):
        try:
            frame = self._capture_region()
            src = self.ocr.read(frame)
            _l, _t, width, height = self.region
            if src:
                if speak:
                    self._queue_line(src, False)
                    self.emit({"event": "status", "text": f"Test OK: {src[:90]}"})
                else:
                    self.emit({"event": "status", "text": f"Obszar {width}×{height}: {src[:90]}"})
            else:
                self.emit({"event": "status", "text": f"Obszar {width}×{height}. Jak pojawi się napis, kliknij Test."})
        except Exception as exc:
            self.emit({"event": "status", "text": f"Zrzut: {exc}"})

    def _send_preview(self, frame):
        if frame is None or frame.size == 0:
            return
        from PIL import ImageEnhance, ImageOps

        image = Image.fromarray(np.ascontiguousarray(frame[:, :, ::-1]))
        image = ImageOps.autocontrast(ImageEnhance.Brightness(image).enhance(1.55))
        image.thumbnail((PREVIEW_W, 160))
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / "preview.png"
        image.save(path, "PNG")
        self.emit({"event": "preview", "file": str(path), "w": image.width, "h": image.height})

    def _probe_auto_band(self):
        """Ręczny pasek długo nic nie widzi (sesja RDR2 17:09: zapisany za wysoko, napisy były niżej) —
        zajrzyj w automatyczny pas u dołu okna; są tam polskie/angielskie zdania → przełącz się na niego."""
        win = self.ps_window
        if not win or self._source_kind() == "chrome":
            return False
        band = window_subtitle_band(win, self._band_profile())
        if self.region and self._region_overlap(self.region, band) > 0.85:
            return False  # to już praktycznie ten sam pas
        saved, self.region = self.region, band
        try:
            text = self.ocr.read(self._capture_region()) or ""
        except Exception:
            text = ""
        finally:
            self.region = saved
        text = trim_ocr_edges(strip_hud_prompts(strip_subtitle_tags(text)))
        if not usable_ocr(text) or looks_like_game_ui(text) or len(text.split()) < 2 \
                or not re.search(r"[.!?…,]", text):
            return False
        self.region = band
        self.lock_region = False
        self.game_regions.pop(self._region_key(), None)
        self.persist()
        self.emit({"event": "status", "text": "Ręczny pasek nie widział napisów — przełączam na automatyczny dół okna."})
        self.emit({"event": "state", **self.snapshot()})
        return True

    def _capture_region(self):
        if self.region is None:
            raise RuntimeError("Brak obszaru.")
        left, top, width, height = [int(v) for v in self.region]
        if width < 8 or height < 8:
            raise RuntimeError("za mały obszar")
        frame = capture_remote_play_band(left, top, width, height, info=self.source_info)
        if frame is not None:
            return frame
        path = os.path.join(tempfile.gettempdir(), f"gamereader_cap_{time.time_ns()}.png")
        err = ""
        try:
            frame = self._capture_ps_window(left, top, width, height, path)
            if frame is not None:
                return frame
            frame = self._capture_mss(left, top, width, height)
            if frame is not None:
                return frame
            bin_path = game_reader_bin()
            if bin_path is not None:
                try:
                    result = subprocess.run(
                        [str(bin_path), "--shot", str(left), str(top), str(width), str(height), path],
                        capture_output=True,
                        text=True,
                        timeout=1.2,
                    )
                    if result.returncode == 0 and os.path.exists(path):
                        with Image.open(path) as image:
                            image.load()
                            return np.array(image.convert("RGB"))[:, :, ::-1].copy()
                    err = (result.stderr or result.stdout or "").strip()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    err = str(exc)
            raise RuntimeError(err or "zrzut nie wszedł")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _capture_ps_window(self, left, top, width, height, path):
        bin_path = game_reader_bin()
        if bin_path is None:
            return None
        try:
            result = subprocess.run(
                [str(bin_path), "--ps-shot", str(left), str(top), str(width), str(height), path],
                capture_output=True,
                text=True,
                timeout=1.2,
            )
            if result.returncode == 0 and os.path.exists(path):
                with Image.open(path) as image:
                    image.load()
                    return np.array(image.convert("RGB"))[:, :, ::-1].copy()
        except (OSError, subprocess.TimeoutExpired):
            return None
        return None

    def _capture_mss(self, left, top, width, height):
        try:
            import mss
        except Exception:
            return None
        try:
            grabber = getattr(mss, "MSS", None) or mss.mss
            with grabber() as sct:
                shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
                arr = np.asarray(shot, dtype=np.uint8)
                if arr.ndim != 3 or arr.shape[0] < 4 or arr.shape[1] < 4:
                    return None
                return np.ascontiguousarray(arr[:, :, :3])
        except Exception:
            return None

    def _queue_line(self, src, translate):
        self._offer_line(src, translate)

    def _prune_spoken(self):
        now = time.monotonic()
        self._spoken_folds = {k: exp for k, exp in self._spoken_folds.items() if exp > now}

    def _worse_reread(self, text):
        """„Wsiądź do metodhet artha” po „Wsiądź do samochodu Amandy.” — ten sam napis, gorszy odczyt."""
        if not ocr_reading_unsettled(text):
            return False
        fold = polish_fold(text)
        rank = ocr_reading_rank(text)
        now = time.monotonic()
        for spoken, exp in getattr(self, "_spoken_texts", {}).items():
            if now >= exp:
                continue
            if SequenceMatcher(None, fold, polish_fold(spoken), autojunk=False).ratio() < 0.55:
                continue
            if rank < ocr_reading_rank(spoken):
                return True
        return False

    def _recently_spoken(self, text):
        fold = polish_fold(text or "")
        if not fold:
            return False
        self._prune_spoken()
        now = time.monotonic()
        if any(now < exp and folds_match(key, fold) for key, exp in self._spoken_folds.items()):
            return True
        # słowo z WIELKĄ literą w środku („MaJEZ”, „weiSe”) to śmieć OCR — nie liczy się do porównania
        words = [w for w in _fold_words(" ".join(t for t in text.split() if not _MIXED_CASE.search(t)))]
        for spoken, exp in getattr(self, "_spoken_texts", {}).items():
            if now >= exp:
                continue
            if ocr_twins(spoken, text):
                return True
            # sam początek przeczytanej już kwestii (napis znika albo wjeżdża): „Stuchaj” po
            # „Stuchaj, mieliśmy z Lamarem ci go zwrócić.”
            said = _fold_words(spoken)
            head = said[: len(words)]
            if words and len("".join(words)) >= 3 and len(head) > 0 and head == words and len(words) < len(said):
                return True
            # albo sam koniec (napis znika po kawałku): „dom”, „dornu.” po „Dostań się do domu.”
            if words and len(words) <= 3 and len(words) < len(said) and _tail_like(words, said[-len(words):]):
                return True
            # ta sama kwestia z doklejonym z brzegu śmieciem (tablica, HUD): „Sas hadrres Przy bani masz…”
            extra = len(words) - len(said)
            if len(said) >= 3 and 0 < extra <= 3 and (
                _tail_like(words[-len(said):], said) or _tail_like(words[: len(said)], said)
            ):
                return True
        return False

    def _mark_spoken(self, text):
        fold = polish_fold(text or "")
        if not fold:
            return
        self._prune_spoken()
        hold = float(self.speak_cooldown or 5.5)
        if len(fold) >= SPOKEN_LONG_FOLD:
            hold = max(hold, SPOKEN_LONG_HOLD_SEC)
        self._spoken_folds[fold] = time.monotonic() + hold
        texts = getattr(self, "_spoken_texts", None)
        if texts is None or len(texts) > 32:
            texts = self._spoken_texts = {}
        now = time.monotonic()
        for key in [k for k, exp in texts.items() if exp <= now]:
            del texts[key]
        texts[text] = self._spoken_folds[fold]
        recent = getattr(self, "_spoken_recent", None)
        if recent is not None:
            recent.append((time.monotonic(), text))

    def _grown_from_recent(self, src, now, window=4.0):
        """Niedawno przeczytana (albo właśnie czytana) kwestia, którą ten napis tylko przedłuża."""
        fold = polish_fold(src)
        recent = [text for at, text in self._spoken_recent if now - at < window] + list(self._speaking_parts)
        for prev in recent:
            left = polish_fold(prev)
            if len(left) >= 8 and len(fold) > len(left) + 1 and fold.startswith(left):
                return prev
        return None

    @property
    def pending(self):
        """Następna kwestia w kolejce (albo None)."""
        queue_ = self._queue
        return queue_[0] if queue_ else None

    def _enqueue(self, item):
        """Dodaj kwestię na koniec kolejki. Nic nie przepada, dopóki kolejka się nie przepełni."""
        src = item[0]
        with self.pending_lock:
            for index, queued in enumerate(self._queue):
                if not same_utterance(src, queued[0]):
                    continue
                # lektor jeszcze tego nie mówi — podmień na czystszy odczyt tej samej kwestii
                if ocr_reading_rank(src) > ocr_reading_rank(queued[0]):
                    self._queue[index] = (src, queued[1], src)
                return
            last = self._queue[-1] if self._queue else None
            if last and (extends_utterance(last[0], src) or (last[2] != last[0] and extends_utterance(last[2], item[2]))):
                self._queue[-1] = item  # ten sam napis, tylko dłuższy (dopisana końcówka)
            else:
                self._queue.append(item)
            if len(self._queue) > SPEECH_QUEUE_MAX:
                self._queue.pop(0)  # lektor zupełnie nie nadąża — najstarsza kwestia przepada
                self._lag = max(self._lag, LAG_CONDENSE_STRONG)
            self.has_pending.set()

    def _offer_tail(self, tail, full):
        """Dopisana końcówka napisu — przeczytaj ją po bieżącej kwestii."""
        tail = strip_fillers(tail)
        if not tail:
            return
        # dopisek angielskiego napisu też trzeba przetłumaczyć
        self._enqueue((tail, should_translate(full), full))

    def _offer_line(self, src, translate):
        src = strip_fillers(src)
        if not src:
            return
        if self.brain is not None:
            self.brain.check(src)  # wynik zwykle gotowy, zanim lektor skończy syntezę
        if self._is_current(src):
            return
        if self._recently_spoken(src):
            return
        # w trakcie czytania: nie ucinaj — najwyżej zapamiętaj nowszą kwestię na potem
        if self.speaking_text:
            if extends_utterance(self.speaking_text, src):
                self.last_subtitle = src
                return
            # nowa kwestia — nie przerywaj bieżącej, tylko ustaw w kolejce (przeczyta po kolei)
            self._enqueue((src, translate, src))
            if not self.no_barge_in:
                self._tts_interrupt.set()
                self.lektor.stop()
            return
        self._enqueue((src, translate, src))

    def _waiting_parts(self):
        """Podgląd tego, co _take_pending weźmie jako następną wypowiedź: (src, translate, części)."""
        with self.pending_lock:
            if not self._queue:
                return None
            first = self._queue[0]
            items = [first] + [it for it in self._queue[1:] if it[1] == first[1]]
        srcs = [it[0] for it in items]
        return (_join_parts(srcs) if len(srcs) > 1 else srcs[0]), first[1], srcs

    def _take_pending(self):
        """Weź WSZYSTKO, co czeka, jako jedną wypowiedź (jak lektor w filmie: bez przerw między
        napisami). Zwraca (src, translate, full, części) albo None."""
        with self.pending_lock:
            if not self._queue:
                self.has_pending.clear()
                return None
            if getattr(self, "catch_up", False) and len(self._queue) > 1:
                # zaległości: kwestia, której napis zniknął dawno temu, i nadmiar ponad limit przepadają —
                # lektor przeskakuje do tego, co jest teraz na ekranie (jak lektor w filmie)
                now = time.monotonic()
                *older, newest = self._queue
                def age(it):
                    return now - self._seen_at.get(strip_fillers(it[0]) or it[0], now)

                keep = [it for it in older if age(it) < CATCH_UP_STALE_SEC and not self._skippable(it[0])] + [newest]
                if len(keep) == 1 and self._skippable(newest[0]):
                    # najnowsze to samo „Do dzieła!” — lepiej przeczytać chwilę starszą kwestię z treścią
                    content = [it for it in older if age(it) < CATCH_UP_STALE_SEC + 1.5 and not self._skippable(it[0])]
                    if content:
                        keep = content[-1:]
                keep = keep[-getattr(self, "catch_up_keep", 2):]
                if len(keep) < len(self._queue):
                    self._timing(f"nadganiam: pomijam {len(self._queue) - len(keep)} zaległe kwestie")
                    self._queue[:] = keep
                    self._prefetch_parts = None
            first = self._queue.pop(0)
            parts = [first]
            # przygotowane zawczasu: weź dokładnie ten zestaw, dla którego dźwięk już czeka
            limit = len(self._prefetch_parts) if self._prefetch_parts and self._prefetch_parts[0] == first[0] else 99
            while len(parts) < limit and self._queue and self._queue[0][1] == first[1]:
                parts.append(self._queue.pop(0))
            self._prefetch_parts = None
            if not self._queue:
                self.has_pending.clear()
        if len(parts) == 1:
            src, translate, full = first
            return src, translate, full, [src]
        srcs = [item[0] for item in parts]
        return _join_parts(srcs), first[1], parts[-1][2], srcs

    def _log_junk(self, text, message):
        """Jeden wpis na napis (OCR czyta ten sam napis kilka razy na sekundę)."""
        if text != self._junk_logged:
            self._junk_logged = text
            self._timing(message)

    def _brain_junk(self, text, wait=0.0):
        """Model decyzji: napis to nie kwestia postaci (menu, znak wodny, powiadomienie, zlepek liter)."""
        if self.brain is None:
            return False
        if self._pl_subs_active and self._like_recent_junk(text):
            self._log_junk(text, f"pomijam — to nie dialog (jak odrzucony przed chwilą): {text[:70]!r}")
            return True
        verdict = self.brain.verdict(text, wait)
        if not verdict:
            return False
        p = verdict["dialog"]
        # przy polskich napisach niepolski tekst z ekranu (szyld, reklama w grze) też jest podejrzany
        suspect = screen_junk_level(text) == "suspect" or (self._pl_subs_active and not looks_polish(text))
        # sam model nie wyrzuca porządnego polskiego zdania (np. kwestii z doklejonym znakiem wodnym)
        if (p < BRAIN_JUNK_P and not looks_polish(text)) or (suspect and p < BRAIN_SUSPECT_P):
            self._log_junk(text, f"pomijam — to nie dialog (model {p:.2f}{', podejrzane' if suspect else ''}): {text[:70]!r}")
            self._remember_junk(text)
            return True
        return False

    def _remember_junk(self, text):
        now = time.monotonic()
        junk = self._junk_words = {w: at for w, at in getattr(self, "_junk_words", {}).items() if now - at < JUNK_MEMORY_SEC}
        for word in _fold_words(text):
            if len(word) >= 5:
                junk[word] = now
        # skrót WIELKIMI literami (logo, „BNR”) — OCR czyta go stale tak samo
        for token in re.findall(r"[A-ZĄĆĘŁŃÓŚŹŻ]{3,}", normalize_text(text)):
            junk["!" + polish_fold(token)] = now

    def _like_recent_junk(self, text):
        """Stały napis z ekranu (nazwa ulicy, szyld), który model już odrzucił, a OCR czyta go za każdym
        razem inaczej („San Andreas BNR” → „Aodreas BNR”, „Medres? BNR”) — bez czekania na model."""
        junk = getattr(self, "_junk_words", None)
        words = _fold_words(text)
        if not junk or not words or len(words) > 4:
            return False
        now = time.monotonic()
        caps = {"!" + polish_fold(t) for t in re.findall(r"[A-ZĄĆĘŁŃÓŚŹŻ]{3,}", normalize_text(text))}
        if any(now - junk.get(tag, -1e9) < JUNK_MEMORY_SEC for tag in caps):
            return True
        if looks_polish(text):
            return False
        return any(
            now - at < JUNK_MEMORY_SEC and SequenceMatcher(None, word, bad, autojunk=False).ratio() >= 0.7
            for word in words if len(word) >= 4
            for bad, at in junk.items() if not bad.startswith("!")
        )

    def _skippable(self, text):
        """Kwestia bez nowej informacji (okrzyk, reakcja, potwierdzenie) — w zrywie wypada pierwsza."""
        verdict = self.brain.verdict(text) if self.brain is not None else None
        if verdict and verdict.get("info") is not None:
            info = verdict["info"]
            return info < BRAIN_SKIP_INFO_P or (is_interjection(text) and info < BRAIN_SKIP_SHORT_P)
        return is_interjection(text)

    def _film_entry(self, src, seen):
        """Jak lektor w filmie: gotowy głos wchodzi FILM_DELAY po tym, jak postać zaczyna mówić.
        Zwraca, ile sekund czekał (0 = od razu, jak dotąd)."""
        vad = self.voice
        t0 = time.monotonic()
        if not vad.alive(t0) or self._lag > FILM_MAX_LAG or self._queue_waiting(src):
            return 0.0
        if not vad.speaking(t0) and t0 - vad.last_voice < FILM_RECENT_VOICE:
            return 0.0  # postać właśnie skończyła — to jej kwestia, czytaj od razu
        deadline = (seen or t0) + FILM_MAX_WAIT
        while not self._tts_interrupt.is_set():
            now = time.monotonic()
            if vad.speaking(now):
                start = vad.onset + FILM_DELAY
                if now >= start:
                    break
                time.sleep(min(0.02, start - now))
                continue
            if now >= deadline or not vad.alive(now) or self._queue_waiting(src):
                break
            time.sleep(0.02)
        return time.monotonic() - t0

    def _tts_loop(self):
        while True:
            if not self.has_pending.wait(timeout=0.15):
                continue
            item = self._take_pending()
            if item is None:
                continue
            src, translate, full, parts = item
            parts = self._fresh_parts(parts)
            # Zawsze zbuduj tekst od nowa: stary src zawiera także części,
            # które powyżej odpadły jako już przeczytane.
            src = _join_parts(parts) if len(parts) > 1 else (parts[0] if parts else "")
            if not parts or same_utterance(src, self.speaking_text):
                continue
            if len(parts) == 1:
                src = parts[0]
            try:
                # od tej chwili kwestia jest „bieżąca” — wariant OCR tego samego napisu, który przyjdzie
                # w trakcie syntezy albo czekania na głos postaci, nie trafi drugi raz do kolejki
                self._speaking_parts = list(parts)
                # przygotowana w tle, gdy lektor kończył poprzednią kwestię — start bez czekania
                t0 = time.monotonic()
                # ta kwestia właśnie syntezuje się na zapas — poczekaj, zamiast robić ją drugi raz
                if len(parts) == 1:
                    with self._spec_done:
                        self._spec_done.wait_for(lambda: self._spec_busy != src, timeout=4.0)
                with self._ready_lock:
                    ready = self._ready.pop(src, None)
                if ready:
                    text, segments, arousal, boost, path = ready
                else:
                    text, segments, arousal, boost = self._plan_line(src, translate, parts)
                    if not segments:
                        continue
                    # pierwsze zdanie od razu — reszta syntezuje się, gdy lektor już mówi
                    path = self.lektor.prepare(
                        segments[0][0], volume=self.lektor_volume / 100.0, mood=segments[0][1], arousal=arousal,
                        hurry=self._queue_waiting(src), boost=boost,
                    )
                # nowy napis: model liczył w czasie syntezy — ostatnie słowo przed odtworzeniem
                # przy polskich napisach niepolski skrawek (nazwa ulicy z HUD) czeka na model dłużej
                odd = self._pl_subs_active and not looks_polish(parts[0])
                if len(parts) == 1 and self._brain_junk(parts[0], wait=BRAIN_ODD_WAIT if odd else 0.15):
                    self._mark_spoken(parts[0])
                    continue
                with self.pending_lock:
                    newer = self.pending
                # przy no_barge_in dokończ obecną syntezę; nowszy zostanie na kolejkę
                if newer and not same_utterance(newer[0], src) and not self.no_barge_in:
                    continue
                self._tts_interrupt.clear()
                seens = [self._seen_at.pop(p, None) for p in parts]
                seen = min((t for t in seens if t), default=None)
                waited = self._film_entry(src, seen)
                if seen:
                    # spóźnienie: szybko rośnie, powoli maleje (fabuła zwalnia = skracanie się wyłącza)
                    lag = time.monotonic() - seen - waited  # celowe wejście z głosem postaci to nie spóźnienie
                    self._lag = lag if lag > self._lag else 0.5 * self._lag + 0.5 * lag
                self._timing(
                    f"{'GOTOWE' if ready else 'synteza'} {time.monotonic() - t0:.2f}s"
                    + (f", od napisu {time.monotonic() - seen:.2f}s" if seen else "")
                    + f", tempo x{boost:.2f}"
                    + (f", połączone {len(parts)}" if len(parts) > 1 else "")
                    + (f", wejście z głosem postaci +{waited:.2f}s" if waited > 0.02 else "")
                    + f" | {text[:70]!r}"
                )
                self.speaking_text = src
                self._speaking_parts = list(parts)
                self._remember_sentences(text)
                if not translate:
                    self.speaking_full = full
                self.last_key = text_key(src)
                self.last_subtitle = src
                self.emit({"event": "line", "text": text, "arousal": round(arousal, 2)})
                self._duck_on()
                gen = self._speech_gen
                # ile jeszcze będzie mówił (szacunek) — do liczenia tempa kwestii przygotowywanych zawczasu
                speak_rate = max(8.0, self.lektor.cps1 * LEKTOR_SPEED / max(0.5, self.lektor.tts_scale) * max(1.0, boost))
                self.lektor.busy_until = time.monotonic() + len(text) / speak_rate + 0.2
                # kwestia zawsze do końca (bez ucinania) — zaległości nadrabia szybsze tempo kolejnych
                # fragmentów i skrót następnej, połączonej wypowiedzi
                for idx in range(len(segments)):
                    self.lektor.play(path)
                    nxt = None
                    if idx + 1 < len(segments):
                        seg, mood = segments[idx + 1]
                        nxt = self.lektor.prepare(
                            seg, volume=self.lektor_volume / 100.0, mood=mood, arousal=arousal,
                            hurry=self._queue_waiting(src), boost=boost,
                        )
                    else:
                        # ostatni fragment gra — w tym czasie przygotuj kolejną kwestię z kolejki
                        self._prefetch_pending(src)
                    if self._wait_segment(src) or gen != self._speech_gen:
                        break
                    path = nxt
                self._mark_spoken(src)
                for part in parts:
                    self._mark_spoken(part)
                if not translate:
                    self._mark_spoken(full)
                    self.last_full = full
            except Exception as exc:
                self.emit({"event": "status", "text": f"Błąd głosu: {exc}"})
            finally:
                self.lektor.busy_until = 0.0
                self.speaking_text = ""
                self._speaking_parts = []
                self.speaking_full = ""
                self._tts_interrupt.clear()
                self._duck_release()

    def _fresh_parts(self, parts):
        """Usuń przeczytane kwestie i warianty OCR także wewnątrz jednej paczki."""
        kept = []
        for part in parts:
            if self._recently_spoken(part) or any(same_utterance(part, prev) for prev in kept):
                continue
            if not self._brain_junk(part):
                kept.append(part)
        return kept

    def _queue_waiting(self, current):
        """Czy w kolejce czeka już następna kwestia — wtedy lektor przyspiesza, żeby jej nie zgubić."""
        with self.pending_lock:
            return any(not same_utterance(item[0], current) for item in self._queue)

    def _queue_len(self, current):
        with self.pending_lock:
            return sum(1 for item in self._queue if not same_utterance(item[0], current))

    def _is_current(self, src):
        """Czy ten napis właśnie czyta lektor (także jako część połączonej wypowiedzi)."""
        return same_utterance(src, self.speaking_text) or any(
            same_utterance(src, part) for part in getattr(self, "_speaking_parts", ())
        )

    def _screen_budget(self, parts):
        """Ile sekund zostało, zanim zniknie najnowszy z napisów (tyle ma lektor na wypowiedź)."""
        # powyżej ~18 zn/s plan i tak wpychał lektora w bełkot; zaległe kwestie i tak wypadają
        cps = min(self._sub_cps or SUBTITLE_CPS_DEFAULT, 18.0)
        # kwestia przygotowywana zawczasu zacznie się dopiero, gdy lektor skończy bieżącą
        now = max(time.monotonic(), self.lektor.busy_until)
        natural = sum(len(p) for p in parts) / cps
        deadline = max(self._seen_at.get(p, now) + len(p) / cps for p in parts)
        # spóźnienie nie skraca budżetu: lektor czyta w tempie napisu, a nie w 65 % tego czasu
        return max(natural, deadline - now, 1.0)

    def _plan_line(self, src, translate, parts=None):
        """Tłumaczenie, podział na zdania i emocja kwestii: (tekst, fragmenty, przejęcie, tempo)."""
        if translate:
            # „Mm-hmm”, „Uh” — przed tłumaczeniem: Argos robi z „Mm-hmm.” czytane „Min-hamm.”, które zajmuje lektora
            clean = strip_fillers(src)
            text = self.translator.translate(clean) if clean else ""
        else:
            text = src
        text = strip_fillers(text)
        segments = self.lektor.plan(text) if text else []
        # dźwięk: cała wypowiedź postaci; napisy: ostatnie ~1,6 s tego, co postać mówi
        arousal = self._heard_arousal.pop(text_key(src), None) if translate else None
        if arousal is None:
            arousal = self.prosody.recent()
        # jak w filmie: wypowiedź ma się zmieścić, zanim zniknie napis z ekranu.
        # Kolejno: szybsze tempo → skrót (wtrącenia, powtórzenia) → mocniejszy skrót →
        # najstarsze zdania wypadają (pierwszeństwo ma to, co jest teraz na ekranie).
        # Przy spokojnej fabule wszystko się mieści i lektor czyta całość.
        seconds = self._screen_budget(parts or [src])
        before = text
        text = self._drop_said_sentences(text)
        # lektor już teraz mówi później niż postać: skracamy nawet kwestię, która „by się zmieściła” —
        # tempo głosu zostaje, a spóźnienie nie narasta kwestia po kwestii
        lag = getattr(self, "_lag", 0.0)
        forced = 2 if lag >= LAG_CONDENSE_STRONG else 1 if lag >= LAG_CONDENSE else 0
        if not translate and parts and len(parts) >= 2 and self.lektor.overload(text, seconds) > 1.0:
            keep = [p for p in parts[:-1] if not self._skippable(p)] + [parts[-1]]
            if len(keep) < len(parts):
                text = strip_fillers(_join_parts(keep)) if len(keep) > 1 else keep[0]
        for level in (1, 2):
            if not text or (level > forced and self.lektor.overload(text, seconds) <= 1.0):
                break
            text = condense_polish(text, level=level)
        sentences = re.findall(r"[^.!?…]+(?:[.!?…]+|$)", text or "")
        total_sentences = len(sentences)
        # całe zdania ze starszych kwestii wypadają przy 2+ zaległych napisach — inaczej ginie kontekst
        min_parts = 2 if getattr(self, "catch_up", False) else 3
        while len(parts or []) >= min_parts and len(sentences) > 1 and self.lektor.overload(" ".join(sentences), seconds) > 1.15:
            # najnowsze zdanie zostaje; najpierw wypadają wtrącenia („Dobra.”, „Jadę!”), potem najstarsze
            sentences.pop(next((i for i, s_ in enumerate(sentences[:-1]) if is_interjection(s_)), 0))
        # jedna kwestia z kilku zdań, która się nie mieści: wtrącenia („Tak.”, „Jadę!”) wypadają przed
        # treścią — inaczej lektor spóźnia się o całe zdanie i nadganianie zjada następny napis
        fits = 0.0 if forced >= 2 else 1.15
        while len(sentences) > 1 and self.lektor.overload(" ".join(sentences), seconds) > fits:
            filler = next((i for i, s_ in enumerate(sentences[:-1]) if is_interjection(s_)), None)
            if filler is None:
                break
            sentences.pop(filler)
        if (len(sentences) > 1 or len(sentences) < total_sentences
                or (sentences and self.lektor.overload(text, seconds) > 1.0)):
            text = normalize_text(" ".join(s.strip() for s in sentences))
        if text != before:
            self._timing(f"skrót (na {seconds:.1f}s): {before[:70]!r} -> {text[:70]!r}")
            segments = self.lektor.plan(text)
        boost = self.lektor.line_boost(text, seconds)
        return text, segments, float(arousal or 0.0), boost

    def _said_fold(self, sentence):
        words = re.findall(r"[\wÀ-ž]+", sentence or "")
        return polish_fold(" ".join(words)) if len(words) >= 3 else ""

    def _remember_sentences(self, text):
        """Zdania właśnie czytanej kwestii — przez SENTENCE_REPEAT_SEC nie wracają w następnej."""
        said = getattr(self, "_said_sentences", None)
        if said is None:
            said = self._said_sentences = {}
        now = time.monotonic()
        for key in [k for k, exp in said.items() if exp <= now]:
            del said[key]
        for sentence in re.findall(r"[^.!?…]+(?:[.!?…]+|$)", text or ""):
            fold = self._said_fold(sentence)
            if fold:
                said[fold] = now + SENTENCE_REPEAT_SEC

    def _drop_said_sentences(self, text):
        """Wyrzuca zdania, które lektor przed chwilą przeczytał. Cała kwestia nigdy nie wypada."""
        said = getattr(self, "_said_sentences", None)
        if not said or not text:
            return text
        now = time.monotonic()
        sentences = [s_.strip() for s_ in re.findall(r"[^.!?…]+(?:[.!?…]+|$)", text) if s_.strip()]
        fresh = [s_ for s_ in sentences if said.get(self._said_fold(s_), 0.0) <= now]
        if not fresh or len(fresh) == len(sentences):
            return text
        return normalize_text(" ".join(fresh))

    def _prefetch_pending(self, current):
        """Gdy lektor czyta ostatni fragment: przetłumacz i zsyntezuj początek następnej kwestii."""
        if not self.no_barge_in:
            return  # bez kolejki nowa kwestia i tak przerywa bieżącą
        peek = self._waiting_parts()
        if not peek:
            return
        src, translate, parts = peek
        if same_utterance(src, current) or all(self._recently_spoken(p) for p in parts):
            return
        with self._ready_lock:
            if src in self._ready:
                return
        try:
            text, segments, arousal, boost = self._plan_line(src, translate, parts)
            if not segments:
                return
            path = self.lektor.prepare(
                segments[0][0], volume=self.lektor_volume / 100.0, mood=segments[0][1], arousal=arousal,
                boost=boost,
            )
            self._store_ready(src, (text, segments, arousal, boost, path))
            self._prefetch_parts = list(parts)
        except Exception:
            pass

    def _store_ready(self, src, item):
        with self._ready_lock:
            if len(self._ready) > 8:
                self._ready.clear()
            self._ready[src] = item

    def _speculate_candidate(self, src, fresh):
        """Czekamy na potwierdzenie napisu — w tym czasie lektor syntezuje najlepszy dotąd odczyt.
        Poprawiony odczyt syntezuje się ponownie tylko, gdy jest czysty (bez zlepków i śmieci)."""
        if fresh:
            self._spec_redo, self._spec_rank = 0, -1
        key = strip_fillers(src)
        if not key or len(key) < 6 or key == getattr(self, "_spec_key", ""):
            return
        # zlepek („altanval Jebany silniki”) albo gorsza powtórka właśnie czytanej kwestii („Z blisko!”
        # po „Za blisko!”) — synteza poszłaby do kosza
        if ocr_junk_count(src) or self._like_spoken(key):
            return
        rank = ocr_reading_rank(src)
        if rank < self._spec_rank or self._spec_redo >= 2:
            return  # gorszy wariant albo już dwie poprawki — CPU zostaje dla gry, lektor syntezuje po zatwierdzeniu
        if not fresh:
            self._spec_redo += 1
        now = time.monotonic()
        prev_key, prev_at = getattr(self, "_spec_last", ("", -1e9))
        if fresh and now - prev_at < 0.35 and SequenceMatcher(
                None, polish_fold(key), polish_fold(prev_key), autojunk=False).ratio() >= 0.7:
            return  # migający wariant tej samej kwestii — dźwięk już się robi
        self._spec_last = (key, now)
        self._spec_key, self._spec_rank = key, rank
        self._seen_at.setdefault(key, getattr(self, "_ocr_started", time.monotonic()))
        if getattr(self, "brain", None) is not None:
            self.brain.check(key)  # ocena modelu gotowa, zanim lektor zacznie — bez czekania 0,15 s
        self._speculate(key, should_translate(src))

    def _like_spoken(self, text):
        fold = polish_fold(text)
        now = time.monotonic()
        spoken = [t for t, exp in getattr(self, "_spoken_texts", {}).items() if exp > now]
        spoken.append(getattr(self, "speaking_text", "") or "")
        return any(
            t and SequenceMatcher(None, fold, polish_fold(t), autojunk=False).ratio() >= 0.8 for t in spoken
        )

    def _speculate(self, src, translate):
        """Zacznij tłumaczyć i syntezować napis od pierwszego odczytu (najnowszy wygrywa)."""
        self._spec_item = (src, translate)
        self._spec_event.set()

    def _spec_loop(self):
        while True:
            self._spec_event.wait()
            self._spec_event.clear()
            item, self._spec_item = self._spec_item, None
            if not item or self.lektor.model is None:
                continue
            src, translate = item
            with self._ready_lock:
                if src in self._ready:
                    continue
            if same_utterance(src, self.speaking_text) or self._recently_spoken(src):
                continue
            self._spec_busy = src
            try:
                text, segments, arousal, boost = self._plan_line(src, translate)
                if segments and self._spec_item is None:  # nowszy odczyt = ten już nieaktualny
                    path = self.lektor.prepare(
                        segments[0][0], volume=self.lektor_volume / 100.0, mood=segments[0][1], arousal=arousal,
                        boost=boost,
                    )
                    self._store_ready(src, (text, segments, arousal, boost, path))
            except Exception:
                pass
            finally:
                with self._spec_done:
                    self._spec_busy = None
                    self._spec_done.notify_all()

    def _timing(self, line):
        log_timing(line)

    def _wait_segment(self, src):
        """Czeka na koniec fragmentu; True = przerwano (Stop albo nowa kwestia przy barge-in)."""
        if self.no_barge_in:
            self.lektor.wait(interrupt_check=self._tts_interrupt.is_set)
        else:
            self.lektor.wait(
                interrupt_check=lambda: self._tts_interrupt.is_set()
                or (self.pending is not None and not same_utterance(self.pending[0] if self.pending else "", src))
            )
        return self._tts_interrupt.is_set()

    def _on_heard(self, heard, audio=None):
        if not heard or heard.lower() in JUNK_HEARD:
            return
        # jak postać to powiedziała — liczone zawsze, żeby miernik uczył się mowy w tej grze
        quiet = audio is not None and self.prosody.is_quiet(audio)
        arousal = self.prosody.analyze(audio) if audio is not None else None
        # „Napisy + dźwięk”: wypowiedź bez napisu (sprawdzone w pętli dźwięku) — tylko pełne zdania
        if self.mode != "audio" and not is_full_sentence(heard):
            return
        convo_only = False
        if self.mode != "audio" and self.brain is not None:
            # tekst piosenki, DJ albo reklama z radia w grze — model decyzji odróżnia je od rozmowy postaci
            self.brain.check(heard, kind="heard")
            verdict = self.brain.verdict(heard, wait=0.4, kind="heard")
            if verdict and verdict["radio"] >= BRAIN_RADIO_P:
                self._timing(f"pomijam dźwięk — radio/piosenka (model {verdict['radio']:.2f}): {heard[:70]!r}")
                return
            unimportant = verdict and verdict.get("important") is not None and verdict["important"] < BRAIN_IMPORTANT_P
            convo_only = bool(unimportant) and time.monotonic() - self._heard_offered_at < HEARD_CONVO_SEC
            if unimportant and not convo_only:
                self._timing(f"pomijam dźwięk — gadanie w tle, nieważne (model {verdict['important']:.2f}): {heard[:70]!r}")
                return
        # ciche mruczenie pod nosem i tłum w tle — pomijamy
        if quiet:
            return
        heard = self._drop_repeated_heard(heard)
        if not heard:
            return
        self.emit({"event": "heard", "text": heard})
        if arousal is not None:
            if len(self._heard_arousal) > 64:
                self._heard_arousal.clear()
            self._heard_arousal[text_key(strip_fillers(heard))] = arousal
        if time.monotonic() < self.subtitle_until:
            return
        if text_key(heard) == self.last_key or self._recently_spoken(heard):
            return
        if not convo_only:
            self._heard_offered_at = time.monotonic()
        self._offer_line(heard, True)

    def _drop_repeated_heard(self, heard):
        """Wypowiedź bez zdań słyszanych niedawno (pętla komunikatu, ta sama odzywka NPC); "" = nic nowego."""
        now = time.monotonic()
        self._heard_seen = {k: at for k, at in self._heard_seen.items() if now - at < HEARD_REPEAT_SEC}
        kept, dropped = [], []
        for sentence in re.split(r"(?<=[.!?])\s+", heard.strip()):
            fold = polish_fold(sentence)
            if len(fold) < HEARD_REPEAT_MIN_FOLD:
                kept.append(sentence)
                continue
            if any(folds_match(fold, seen) for seen in self._heard_seen):
                dropped.append(sentence)
            else:
                kept.append(sentence)
            self._heard_seen[fold] = now
        if dropped:
            self._timing(f"pomijam dźwięk — powtórka (pętla/odzywka): {' '.join(dropped)[:70]!r}")
        if not any(len(polish_fold(s)) >= HEARD_REPEAT_MIN_FOLD for s in kept):
            return "" if dropped else heard
        return " ".join(kept)

    def _unsubtitled_speech(self, audio, start, end):
        """Czy tę wypowiedź z dźwięku gry tłumaczyć: w trybie „Dźwięk” zawsze; w „Napisy + dźwięk” tylko
        wyraźną, z głosem, długą jak zdanie i taką, w trakcie której na ekranie nie było napisu."""
        if self.mode == "audio":
            return True
        if self.mode != "auto":
            return False
        if self._last_subtitle_seen >= start - AUDIO_SUBTITLE_MARGIN:
            return False  # napis był w trakcie — dialog czyta się z napisu
        if (end - start) < AUDIO_MIN_SPEECH_SEC or self.prosody.is_quiet(audio):
            return False
        if voiced_fraction(audio) < AUDIO_MIN_VOICED:
            return False
        # dźwięk bez żadnej pauzy (muzyka w lokalu, radio): postać może mówić na tle muzyki (fryzjer,
        # sklep) — nie odrzucamy na ślepo; tekst piosenki, DJ i reklamy odsiewa potem model decyzji
        if audio.size / float(SAMPLE_RATE) >= MAX_SPEECH_SEC - 0.05:
            return True
        sung = sung_fraction(audio)
        if sung >= AUDIO_SUNG_MAX:
            self._log_radio(f"śpiew ({sung:.2f})")
            return False
        return True

    def _log_radio(self, why):
        now = time.monotonic()
        if now - getattr(self, "_radio_logged_at", 0.0) > 20.0:
            self._radio_logged_at = now
            self._timing(f"pomijam dźwięk — {why}")

    def _flush_line_q(self):
        try:
            while True:
                self.line_q.get_nowait()
        except queue.Empty:
            pass
        with self.pending_lock:
            self._queue.clear()
            self.has_pending.clear()
        self._speech_gen += 1
        self._tts_interrupt.set()
        self.lektor.stop()
        self._tts_interrupt.clear()

    @property
    def _pl_subs_active(self):
        """Gra ma polskie napisy — lektor czyta tylko je, angielskiej mowy nie tłumaczy."""
        at = self._pl_subs_at
        return self.mode != "audio" and at is not None and time.monotonic() - at < PL_SUBS_HOLD_SEC

    def _on_subtitle(self, src):
        src = strip_subtitle_tags(src, lowercase_only=True)
        if not usable_ocr(src):
            return
        if self._source_kind() != "chrome" and looks_like_game_ui(src):
            # menu, koło wyboru broni, podpowiedzi przycisków, liczniki — lektor czyta tylko dialogi
            if src != getattr(self, "_ui_logged", ""):
                self._ui_logged = src
                self.emit({"event": "debug", "text": f"pomijam interfejs gry: {src}"})
            return
        # „[hiszpański]”, „[śmiech]” — opis dla niesłyszących, nie kwestia (podpowiedzi „[E]” odpadły wyżej)
        src = strip_subtitle_tags(src)
        if not usable_ocr(src):
            return
        if self._source_kind() == "chrome":
            if player_paused(src):
                return
            src = strip_player_ui(src)
            if not usable_ocr(src) or is_player_ui_text(src):
                return
        src = trim_ocr_edges(src)
        unhud = strip_hud_prompts(src)
        if unhud != normalize_text(src):
            self._log_junk(src, f"wycinam podpowiedzi przycisków: {src[:70]!r} -> {unhud[:70]!r}")
            src = trim_ocr_edges(unhud)
            if not usable_ocr(src) or not re.search(r"[a-ząćęłńóśźż]{2}", src):
                return
        lead = getattr(self, "_lead_junk", None)
        if lead is None:
            store = CACHE_DIR / "lead_junk.json" if hasattr(self, "lektor") else None  # testy bez zapisu
            lead = self._lead_junk = RecurringLead(store=store)
        lead.observe(src)
        unled = lead.strip(src)
        if unled != normalize_text(src):
            self._log_junk(src, f"wycinam etykietę z mapy: {src[:70]!r} -> {unled[:70]!r}")
            src = unled
            if not usable_ocr(src):
                return
        if self._worse_reread(src):
            self._log_junk(src, f"pomijam — zła powtórka przeczytanej kwestii: {src[:70]!r}")
            return
        # „Thy co wedy atak eles.” — zła klatka właśnie przeczytanego napisu, bez ani jednego polskiego słowa
        if (not should_translate(src) and ocr_reading_rank(src) <= 0
                and len(re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+", src)) >= 3):
            self._log_junk(src, f"pomijam — odczyt bez polskich słów: {src[:70]!r}")
            return
        if screen_junk_level(src) == "junk":
            self._log_junk(src, f"pomijam — to nie dialog (reguła): {src[:70]!r}")
            return
        if looks_like_plate(src):
            self._log_junk(src, f"pomijam — tablica rejestracyjna: {src[:70]!r}")
            self._remember_junk(src)
            return
        if self._pl_subs_active and lone_hud_word(src):
            self._log_junk(src, f"pomijam — samo słowo z HUD: {src[:70]!r}")
            return
        unjunked = strip_lead_junk(src)
        if unjunked != normalize_text(src):
            self._log_junk(src, f"wycinam śmieć przed napisem: {src[:70]!r} -> {unjunked[:70]!r}")
            src = unjunked
        unplated = strip_plate(src)
        if unplated != normalize_text(src):
            self._log_junk(src, f"wycinam tablicę rejestracyjną: {src[:70]!r} -> {unplated[:70]!r}")
            src = unplated
            if not usable_ocr(src):
                return
        stripped = self._recurring.strip(src)
        if stripped != src:
            if stripped:
                self._log_junk(src, f"wycinam znak wodny: {src[:70]!r} -> {stripped[:70]!r}")
            else:
                self._log_junk(src, f"pomijam — to nie dialog (znak wodny): {src[:70]!r}")
            src = stripped
            if looks_like_plate(src):
                return
        if not usable_ocr(src):
            return
        src = self._strip_known_prefix(src)
        self._last_subtitle_seen = time.monotonic()
        now = time.monotonic()
        base = self.speaking_full or self.last_full
        # napis dopisał resztę zdania — doczytaj tylko końcówkę, nic nie ucinaj
        if base and extends_utterance(base, src):
            self.last_subtitle = src
            self.subtitle_until = now + 2.5
            tail = utterance_tail(base, src)
            if _speakable(tail) and not self._recently_spoken(tail):
                self._offer_tail(tail, src)
            return
        # napis urósł względem jednej z ostatnich kwestii (OCR złapał najpierw początek, a lektor zdążył
        # przeczytać coś innego) — doczytaj tylko nowe słowa; tylko gdy napis zaczyna się tak samo
        grown = self._grown_from_recent(src, now)
        if grown:
            self.last_subtitle = src
            self.subtitle_until = now + 2.5
            tail = utterance_tail(grown, src)
            if _speakable(tail) and not self._recently_spoken(tail):
                self._offer_tail(tail, src)
            return
        if (base and same_utterance(src, base)) or self._recently_spoken(src):
            self.last_subtitle = src
            self.subtitle_until = now + 2.5
            self._ocr_candidate = ""
            self._ocr_candidate_n = 0
            self._ocr_samples = []
            return
        # czekaj, aż napis przestanie się zmieniać (pisanie literka po literce, druga linia)
        need = max(1, int(getattr(self, "confirm_frames", OCR_CONFIRM_FRAMES)))
        if self._source_kind() == "chrome":
            need = 1  # Netflix/YouTube pokazują napis od razu w całości — jeden odczyt wystarczy
        elif not re.search(r"[.!?…]$", src) and (len(src.split()) <= 3 or not looks_polish(src)):
            # Migające fragmenty tablic („Andersi,”, „ta Had”, „Have”) potrafią
            # dostać wysoką ocenę modelu. Wymagaj drugiego odczytu (~150 ms),
            # zamiast czytać pojedynczą błędną klatkę.
            need = max(2, need)
        samples = getattr(self, "_ocr_samples", None) or []
        started = getattr(self, "_ocr_started", 0.0)
        if (self._ocr_candidate and now - getattr(self, "_ocr_candidate_at", -1e9) < 0.6
                and same_utterance(src, self._ocr_candidate) and not extends_utterance(self._ocr_candidate, src)):
            self._ocr_candidate_n += 1
            samples.append(src)
        elif (samples and now - started < OCR_SETTLE_SEC and ocr_same_flicker(src, self._ocr_candidate)
                and not extends_utterance(self._ocr_candidate, src)):
            # „z mógł kantować…” i „Wtedy będziesz mógł kantować…” to nadal ta sama klatka
            self._ocr_candidate_n += 1
            samples.append(src)
        else:
            samples = [src]
            self._ocr_candidate = src
            self._ocr_candidate_n = 1
            self._ocr_started = now
            key = strip_fillers(src)
            if key:
                if len(self._seen_at) > 32:
                    self._seen_at.clear()
                self._seen_at.setdefault(key, now)
        self._ocr_samples = samples
        if samples:
            # przy remisie wygrywa wariant czytany najczęściej („klama” ×6 nad „klarna” ×2), potem
            # późniejsza klatka — pierwsza bywa najgorsza
            self._ocr_candidate = max(
                enumerate(samples),
                key=lambda item: (
                    ocr_reading_rank(item[1]), samples.count(item[1]),
                    sum(ch in PL_MARK for ch in item[1]), item[0],
                ),
            )[1]
        self._ocr_candidate_at = now
        src = self._ocr_candidate
        age = now - getattr(self, "_ocr_started", now)
        game = self._source_kind() != "chrome"
        if (self._ocr_candidate_n < need or (game and len(src.split()) >= 2 and age < OCR_HOLD_SEC)
                or (game and len(src) >= 24 and ocr_reading_unsettled(src) and age < OCR_SETTLE_SEC)):
            self._speculate_candidate(src, fresh=len(samples) == 1)
            return
        self._ocr_candidate = ""
        self._ocr_candidate_n = 0
        self._ocr_samples = []
        self.last_subtitle = src
        self.last_key = text_key(src)
        self.subtitle_until = now + 2.5
        # jak długo wisiał poprzedni napis = ile czasu postać go mówiła (przy ciągłej rozmowie)
        if self._sub_prev:
            prev_t, prev_len = self._sub_prev
            gap = now - prev_t
            if 0.6 < gap < 8.0 and prev_len >= 8:
                cps = prev_len / gap
                if 5.0 <= cps <= 40.0:
                    # mediana z ostatnich pomiarów — pojedynczy błędny odczyt OCR nie zawyża tempa
                    self._sub_cps_samples = (self._sub_cps_samples + [cps])[-9:]
                    if len(self._sub_cps_samples) >= 3:
                        self._sub_cps = float(np.median(self._sub_cps_samples))
        self._sub_prev = (now, len(src))
        # napisy po angielsku (gra albo Netflix bez PL) — tłumacz automatycznie
        translate = should_translate(src)
        if not translate:
            if not self._pl_subs_active:
                self.emit(
                    {"event": "status", "text": "Polskie napisy — czytam je; z dźwięku tłumaczę tylko pełne zdania bez napisu."
                     if self.mode == "auto" else "Polskie napisy — czytam je, angielski dźwięk tylko do emocji i ściszania."}
                )
            self._pl_subs_at = now
        self._offer_line(src, translate)

    def _strip_known_prefix(self, src):
        """Śmieć z kadru doklejony przed kwestią, którą OCR już widział czysto — wycięty. Gdy to czekający
        napis miał doklejony przód, a teraz przyszedł czysty odczyt — próbki też tracą ten przód."""
        now = time.monotonic()
        known = [self._ocr_candidate, *(getattr(self, "_ocr_samples", None) or []), getattr(self, "speaking_text", "") or ""]
        known += [text for text, exp in getattr(self, "_spoken_texts", {}).items() if exp > now]
        for line in known:
            tail = strip_known_prefix(src, line) if line else None
            if tail:
                self._log_junk(src, f"wycinam śmieć przed znaną kwestią: {src[:70]!r} -> {tail[:70]!r}")
                return tail
        samples = getattr(self, "_ocr_samples", None) or []
        cleaned = [strip_known_prefix(sample, src) or sample for sample in samples]
        if cleaned != samples:
            self._ocr_samples = cleaned
            self._ocr_candidate = strip_known_prefix(self._ocr_candidate, src) or self._ocr_candidate
        return src

    def _flush_candidate(self):
        """Czysty napis przeczytany raz, a w następnej klatce już HUD / śmieć / pusto („Zabieraj stąd
        swoje dupsko.” → „Mitun”) — po czasie zatrzymania lektor i tak go czyta, zamiast zgubić."""
        src = self._ocr_candidate
        if not src or self._source_kind() == "chrome":
            return
        now = time.monotonic()
        if now - getattr(self, "_ocr_candidate_at", now) < 0.05:
            return  # ta klatka właśnie go dotknęła — decyzja już zapadła w _on_subtitle
        age = now - getattr(self, "_ocr_started", now)
        if not OCR_HOLD_SEC <= age < 2.0:
            return
        if not re.search(r"[.!?…]$", src) or not looks_polish(src) or ocr_reading_unsettled(src):
            return
        self._ocr_candidate_at = now  # liczy się jak kolejny odczyt tej samej klatki
        self._on_subtitle(src)

    def _ocr_scan_thread(self):
        try:
            self._ocr_scan_loop()
        finally:
            self.emit({"event": "running", "on": False})

    def _warn_ocr_language(self):
        """Windows bez polskiego OCR czyta bez ąęćłńóśźż — powiedz raz, jak to naprawić."""
        backend = getattr(self.ocr, "backend", None)
        if self._ocr_lang_warned or backend is None:
            return
        if backend.error:
            self._ocr_lang_warned = True
            self.emit({"event": "status", "text": backend.error})
        elif backend.missing_polish:
            self._ocr_lang_warned = True
            self.emit(
                {
                    "event": "status",
                    "text": "Windows nie ma polskiego OCR — napisy bez polskich znaków. Ustawienia → Czas i język → "
                    "Język i region → Dodaj język: polski, potem uruchom LiveDub ponownie.",
                }
            )
            winplat.open_language_settings()

    def _ocr_only_loop(self):
        # bez tłumaczenia z dźwięku: emocje z głosu postaci (zawsze) i ściszanie gry (gdy włączone)
        threading.Thread(target=self._audio_loop_ps_remote, kwargs={"transcribe": False}, daemon=True).start()
        try:
            self._ocr_scan_loop()
        finally:
            self.emit({"event": "running", "on": False})

    def _hybrid_loop(self):
        threading.Thread(target=self._ocr_scan_loop, daemon=True).start()
        self._audio_loop()

    def _ocr_scan_loop(self):
        last_hash = ""
        empty_streak = 0
        last_err = ""
        while self.running:
            started = time.time()
            try:
                self._sync_remote_band()
                # przeglądarka: przy widocznym pasku sterowania nie czytaj (tytuł filmu to nie dialog)
                if self._source_kind() == "chrome" and player_controls_visible(self.source_info):
                    now = time.monotonic()
                    if now - self._controls_logged > 5.0:
                        self._controls_logged = now
                        self.emit({"event": "debug", "text": "pasek sterowania odtwarzacza widoczny — pomijam klatki"})
                    time.sleep(max(MIN_INTERVAL, self.interval))
                    continue
                frame = self._capture_region()
                last_err = ""
                digest = hashlib.sha1(frame.tobytes()).hexdigest()[:20]
                if digest != last_hash or not self.last_subtitle:
                    last_hash = digest
                    src = self.ocr.read(frame)
                    if IS_WIN:
                        self._warn_ocr_language()
                    if src and src != self._last_ocr_logged:
                        self._last_ocr_logged = src
                        self.emit({"event": "debug", "text": f"OCR: {src}"})
                    if src:
                        empty_streak = 0
                        self._on_subtitle(src)
                        self._flush_candidate()
                    else:
                        self._flush_candidate()
                        empty_streak += 1
                        # nie czyść last_subtitle zbyt szybko — inaczej ta sama kwestia leci w kółko
                        if empty_streak >= 18 and not self.speaking_text:
                            self.last_subtitle = ""
                            self.last_full = ""
                        # zapisany pasek gry: co ~3 s pustki zajrzyj w automatyczny pas u dołu okna
                        if empty_streak >= 20 and empty_streak % 20 == 0 and self.lock_region \
                                and self._region_key() in self.game_regions and self._probe_auto_band():
                            empty_streak = 0
                            last_hash = ""
                        if empty_streak >= 40 and self.lock_region and self._region_key() not in self.game_regions:
                            self.lock_region = False
                            self.persist()
                            self._sync_remote_band(force=True)
                            empty_streak = 0
                            last_hash = ""
                            self.emit({"event": "status", "text": "Ręczny pasek nic nie widzi — wracam do automatu."})
                elif self._ocr_candidate:
                    # ta sama klatka = ten sam tekst: potwierdź czekający napis bez ponownego OCR
                    # (Netflix: wideo czarne przez DRM, więc przy stojącym napisie obraz się nie zmienia)
                    self._on_subtitle(self._ocr_candidate)
            except Exception as exc:
                text = str(exc).strip() or "błąd"
                if "timed out" in text.lower() or "timeout" in text.lower():
                    text = "zrzut się zacina — próbuję dalej"
                if text != last_err:
                    last_err = text
                    self.emit({"event": "status", "text": f"Napisy: {text}"})
            wait = max(MIN_INTERVAL, min(self.interval, 0.8)) - (time.time() - started)
            if wait > 0:
                time.sleep(wait)

    def _warmup_translator(self):
        try:
            self.translator.translate("Hello there.")
        except Exception as exc:
            self.emit({"event": "status", "text": f"Tłumacz nie wstaje: {exc}"})

    def _audio_loop(self):
        self.emit({"event": "status", "text": "Podpinam dźwięk w tle…"})
        threading.Thread(target=self._warmup_translator, daemon=True).start()
        self.transcriber = LiveTranscriber(
            self.stt,
            self._on_heard,
            on_error=lambda msg: self.emit({"event": "status", "text": f"Błąd STT: {msg}"}),
            on_ready=lambda: self.emit({"event": "status", "text": "Nasłuch EN→PL w tle. Napisy PL mają pierwszeństwo."}),
        )
        self.transcriber.start()
        try:
            self._audio_loop_ps_remote()
        finally:
            if self.transcriber is not None:
                self.transcriber.stop()
                self.transcriber = None
        self.emit({"event": "running", "on": False})

    def _audio_loop_ps_remote(self, transcribe=True):
        tap = GameAudioTap("chrome" if self._source_kind() == "chrome" else "ps", duck=self.duck)
        live = self.transcriber if transcribe else None
        try:
            tap.start()
            self._tap = tap
            duck_note = " · ściszam grę, gdy mówi lektor" if tap.ducking else ""
            if self.duck and not tap.ducking:
                duck_note = " · ściszanie niedostępne"
            if transcribe:
                self.emit({"event": "status", "text": f"Słucham {self._source_label()}{duck_note}."})
            state = UtteranceCutter()
            while self.running:
                chunk = tap.read(0.03)
                if chunk is None:
                    dead = (tap.proc is not None and tap.proc.poll() is not None) or (
                        tap.sock is not None and not tap.alive
                    )
                    if dead:
                        raise RuntimeError(tap.message or "Źródło dźwięku się rozłączyło.")
                    continue
                self.prosody.feed(chunk)
                self.voice.feed(chunk)
                if live is None:
                    continue
                audio = state.feed(chunk)
                if audio is not None:
                    # koniec wypowiedzi = przed chwilą ciszy, która ją zamknęła; początek — minus jej długość
                    end = time.monotonic() - SILENCE_SEC
                    start = end - audio.size / float(SAMPLE_RATE) + PREROLL_SEC
                    if self._unsubtitled_speech(audio, start, end):
                        live.submit(audio)
                    elif not self.prosody.is_quiet(audio):
                        # dialog z napisem: nie rozpoznajemy (GPU dla lektora), tylko miernik emocji się uczy
                        self.prosody.analyze(audio)
        except PermissionError as exc:
            self.emit({"event": "perm", "text": str(exc)})
            if transcribe:
                self.running = False
        except Exception as exc:
            text = str(exc)
            if any(word in text.lower() for word in ("tcc", "zgody", "przechwytywania", "denied", "not permitted")):
                self.emit({"event": "perm", "text": text})
            elif transcribe:
                self.emit({"event": "status", "text": f"Dźwięk gry: {exc}"})
            else:
                self.emit({"event": "status", "text": f"Dźwięk gry (emocje, ściszanie) niedostępny: {exc}"})
            # w trybie „Napisy” problem z dźwiękiem nie wyłącza czytania napisów
            if transcribe:
                self.running = False
        finally:
            self._tap = None
            tap.stop()
