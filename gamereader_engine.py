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
from pathlib import Path

import numpy as np
from PIL import Image


POLL_MS = 80
MIN_INTERVAL = 0.10
DEFAULT_INTERVAL = 0.14
OCR_CONFIRM_FRAMES = 2


def extends_utterance(prev, nxt):
    """Nowy napis to rozwinięcie poprzedniego (OCR dogląda reszty zdania)."""
    left = polish_fold(prev or "")
    right = polish_fold(nxt or "")
    if not left or not right or len(right) <= len(left) + 1:
        return False
    return right.startswith(left) or (left in right and len(right) >= int(len(left) * 1.2))
BLACK_MEAN = 8.0
PREVIEW_W = 780
PREVIEW_H = 110
CONFIG_PATH = Path.home() / "Library/Application Support/GameReader/config.json"
CACHE_DIR = Path.home() / "Library/Caches/GameReader"
VOICE_DIR = Path.home() / "Library/Application Support/GameReader/voices"
JARVIS_ONNX = VOICE_DIR / "pl_PL-jarvis_wg_glos-medium.onnx"
CHATTERBOX_REF = VOICE_DIR / "chatterbox_ref_pl.wav"

MOODS = {
    # length < 1 = szybciej — nie schodź za nisko, bo brzmi jak przyspieszone nagranie
    "narrate": {"label": "spokojny", "length": 1.05, "volume": 0.95, "noise": 0.45, "noise_scale": 0.55, "pause": 0.10, "exag": 0.45, "cfg": 0.50},
    "urgent": {"label": "ostry", "length": 0.88, "volume": 1.22, "noise": 0.90, "noise_scale": 0.75, "pause": 0.06, "exag": 0.72, "cfg": 0.32},
    "intense": {"label": "gniew", "length": 0.82, "volume": 1.30, "noise": 1.05, "noise_scale": 0.90, "pause": 0.05, "exag": 0.85, "cfg": 0.28},
    "question": {"label": "pytanie", "length": 1.16, "volume": 1.08, "noise": 0.70, "noise_scale": 0.65, "pause": 0.12, "exag": 0.58, "cfg": 0.40},
    "soft": {"label": "cicho", "length": 1.40, "volume": 0.55, "noise": 0.25, "noise_scale": 0.35, "pause": 0.22, "exag": 0.30, "cfg": 0.55},
    "tender": {"label": "ciepło", "length": 1.22, "volume": 0.84, "noise": 0.35, "noise_scale": 0.40, "pause": 0.14, "exag": 0.48, "cfg": 0.48},
    "dark": {"label": "mrocznie", "length": 1.30, "volume": 0.74, "noise": 0.85, "noise_scale": 0.75, "pause": 0.16, "exag": 0.64, "cfg": 0.34},
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
def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def text_key(text):
    return hashlib.sha1(normalize_text(text).lower().encode("utf-8")).hexdigest()


def same_utterance(a, b):
    left = normalize_text(a or "").lower()
    right = normalize_text(b or "").lower()
    if not left or not right:
        return False
    if left == right:
        return True
    fold_a = polish_fold(left)
    fold_b = polish_fold(right)
    if not fold_a or not fold_b:
        return False
    if fold_a == fold_b:
        return True
    short, long = (fold_a, fold_b) if len(fold_a) <= len(fold_b) else (fold_b, fold_a)
    # Hogwarts OCR miga — traktuj prawie-to-samo jako to samo
    if len(short) >= 8 and short in long and len(short) / len(long) >= 0.72:
        return True
    if abs(len(fold_a) - len(fold_b)) <= 4 and min(len(fold_a), len(fold_b)) >= 8:
        return _lev(fold_a, fold_b) <= 3
    return False


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


def _normalize_polish_punct(text):
    raw = text or ""
    raw = raw.replace("…", "…").replace("...", "…").replace("..", "…")
    raw = raw.replace("！", "!").replace("？", "?")
    raw = raw.replace("„", "\"").replace("”", "\"").replace("«", "\"").replace("»", "\"")
    raw = re.sub(r"\s+([!?…,.;:])", r"\1", raw)
    raw = re.sub(r"([!?…]){2,}", lambda m: m.group(0)[0] if m.group(0)[0] in "…" else m.group(0)[:2], raw)
    return normalize_text(raw)


def repair_polish_ocr(text):
    """Poprawia błędy OCR PL: śmieci, zgubione diakrytyki, «ze»→«że»."""
    raw = _normalize_polish_punct(text)
    if not raw:
        return ""
    dialogue = bool(re.search(r"[!?…]", raw)) or len(raw) >= 18
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


def looks_polish(text):
    raw = normalize_text(text)
    if not raw:
        return False
    if any(ch in PL_MARK for ch in raw):
        return True
    tokens = [polish_fold(part) for part in re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+", raw)]
    hits = 0
    for token in tokens:
        if token in _POLISH_BY_FOLD or token in _POLISH_FOLDED:
            hits += 1
            continue
        if any(key.startswith(token) or token.startswith(key) for key in _POLISH_BY_FOLD if len(token) >= 4 and len(key) >= 4):
            hits += 1
    return hits >= 1


def should_translate(text):
    raw = normalize_text(text)
    if not raw or looks_polish(raw):
        return False
    tokens = set(re.findall(r"[a-z']+", raw.lower()))
    return len(tokens & EN_HINTS) >= 1 or (len(tokens) >= 4 and not looks_polish(raw))


def usable_ocr(text):
    raw = normalize_text(text)
    if len(raw) < 2:
        return False
    if re.fullmatch(r"[\d\s:%./+\-–—]+", raw):
        return False
    return sum(ch.isalpha() for ch in raw) >= 2


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


def mood_from_punct(chunk):
    """Emocja wyłącznie z końcówki / znaków w fragmencie."""
    raw = normalize_text(chunk or "")
    if not raw:
        return "narrate"
    if re.search(r"\?[!？]|[!！]\?", raw) or ("?!" in raw) or ("!?" in raw):
        return "intense"
    bangs = raw.count("!") + raw.count("！")
    if bangs >= 2:
        return "intense"
    if bangs == 1:
        return "urgent"
    if "?" in raw or "？" in raw:
        return "question"
    if "…" in raw or "..." in raw or raw.rstrip(".!?").endswith(".."):
        return "soft"
    if raw.endswith(",") or raw.endswith(";") or raw.endswith("—") or raw.endswith("–"):
        return "narrate"
    if raw.endswith("."):
        return "narrate"
    return "narrate"


def split_emotion_segments(text):
    """Dzieli napis na kawałki według interpunkcji — każdy z własną emocją."""
    raw = normalize_text(text)
    if not raw:
        return []
    # zachowaj znaki; dziel po silnej interpunkcji
    parts = re.split(r"(?<=[\.\!\?…！？])\s+|(?<=\.\.\.)\s+", raw)
    segments = []
    for part in parts:
        piece = normalize_text(part)
        if not piece:
            continue
        # doklej samotne wielokropki do poprzedniego
        if piece in ("...", "…") and segments:
            prev_text, prev_mood = segments[-1]
            segments[-1] = (normalize_text(prev_text + " " + piece), "soft")
            continue
        mood = mood_from_punct(piece)
        segments.append((piece, mood))
    if not segments:
        segments = [(raw, mood_from_punct(raw))]
    return segments


def detect_mood(polish, english=None):
    """Główna emocja kwestii: najpierw interpunkcja, potem słowa."""
    raw = normalize_text(" ".join(part for part in (polish, english) if part))
    if not raw:
        return "narrate"

    # Dominująca emocja z segmentów (ważniejsze = intensywniejsze)
    priority = {"intense": 5, "urgent": 4, "question": 3, "soft": 2, "tender": 2, "dark": 2, "narrate": 1}
    segments = split_emotion_segments(raw)
    best = "narrate"
    best_p = 0
    for _piece, mood in segments:
        p = priority.get(mood, 0)
        if p > best_p:
            best = mood
            best_p = p

    # Słowa mogą podbić tender/dark gdy brak mocnej interpunkcji
    scores = {name: 0 for name in MOODS}
    if best != "narrate":
        # Interpunkcja (! ? …) zawsze ustala emocję głosu
        return best
    _score_words(scores, polish, MOOD_WORDS_PL)
    _score_words(scores, english, MOOD_WORDS_EN)
    picked = max(scores, key=scores.get)
    if scores[picked] <= 0:
        return "narrate"
    return picked


def mood_voice(mood, intensity=1.0, tts_scale=1.0):
    """Parametry głosu Pipera dla emocji (+ suwak Emocje)."""
    base = MOODS.get(mood) or MOODS["narrate"]
    calm = MOODS["narrate"]
    # intensity 0 = prawie spokojny, 1 = pełny profil, >1 = jeszcze ostrzej
    t = max(0.0, min(1.5, float(intensity)))
    blend = min(1.0, 0.35 + 0.75 * t)

    def mix(a, b):
        return float(a) + (float(b) - float(a)) * blend

    length = mix(calm["length"], base["length"]) * float(tts_scale or 1.0)
    # nie przyspieszaj agresywnie — lepiej lekko wolniej niż „pisk”
    if t > 1.0 and mood in ("urgent", "intense"):
        length *= 0.96
    if t > 1.0 and mood in ("soft", "question"):
        length *= 1.04
    volume = mix(calm["volume"], base["volume"]) * (0.88 + 0.18 * min(t, 1.0))
    noise = mix(calm["noise"], base["noise"])
    noise_scale = mix(calm.get("noise_scale", 0.55), base.get("noise_scale", 0.55))
    pause = float(base.get("pause", 0.08))
    return {
        "length": max(0.78, min(1.7, length)),
        "volume": max(0.45, min(1.40, volume)),
        "noise": max(0.15, min(1.3, noise)),
        "noise_scale": max(0.2, min(1.1, noise_scale)),
        "pause": pause,
        "label": base["label"],
    }


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


def normalize_mode(mode):
    if mode in ("audio", "ocr"):
        return mode
    return "auto"


GAME_PROFILES = {
    "rdr2": {
        "label": "Red Dead Redemption 2",
        "hint": "Szybkie kwestie na dole. Skan 0,16 s, lektor żwawy.",
        "interval": 0.16,
        "tts": 0.92,
        "boost": 2.6,
        "band": 0.18,
        "gap": 0.03,
        "inset": 0.10,
    },
    "tlou": {
        "label": "The Last of Us",
        "hint": "Czytelne napisy na dole, trochę dłuższe kwestie.",
        "interval": 0.18,
        "tts": 0.96,
        "boost": 2.2,
        "band": 0.12,
        "gap": 0.03,
        "inset": 0.10,
    },
    "gow": {
        "label": "God of War",
        "hint": "Duże napisy, średni skan, spokojniejszy lektor.",
        "interval": 0.20,
        "tts": 1.00,
        "boost": 2.0,
        "band": 0.14,
        "gap": 0.03,
        "inset": 0.08,
    },
    "gta": {
        "label": "GTA / Uncharted",
        "hint": "Szybkie dialogi, krótki pasek, szybki skan.",
        "interval": 0.15,
        "tts": 0.90,
        "boost": 2.4,
        "band": 0.11,
        "gap": 0.03,
        "inset": 0.12,
    },
    "souls": {
        "label": "Souls / Elden Ring",
        "hint": "Mały tekst, mocniejszy kontrast, szybki skan.",
        "interval": 0.15,
        "tts": 0.95,
        "boost": 2.8,
        "band": 0.10,
        "gap": 0.025,
        "inset": 0.16,
    },
    "hogwarts": {
        "label": "Hogwarts Legacy",
        "hint": "Żółte imię pomijam. Dokańczam zdanie — bez ucinania i zapętleń.",
        "interval": 0.24,
        "tts": 1.12,
        "boost": 2.3,
        "band": 0.17,
        "gap": 0.028,
        "inset": 0.12,
        "skip_yellow_speaker": True,
        "confirm_frames": 2,
        "speak_cooldown": 7.0,
        "no_barge_in": True,
        "ocr_accurate_first": True,
        "ocr_langs": ["pl-PL"],
    },
    "generic": {
        "label": "Inna gra",
        "hint": "Uniwersalny skan. Jak nie łapie — zaznacz pasek ręcznie.",
        "interval": 0.22,
        "tts": 1.00,
        "boost": 2.2,
        "band": 0.15,
        "gap": 0.035,
        "inset": 0.10,
    },
}


def normalize_game(game):
    if game in GAME_PROFILES:
        return game
    return "rdr2"


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


def capture_remote_play_band(left, top, width, height):
    """Szybki zrzut paska z okna PS (Quartz w procesie, bez spawn helpera)."""
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
    info = _quartz_remote_play_info()
    if info is None:
        return None
    wx, wy, ww, wh, wid = info
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
        Path.home() / "Applications/GameReader.app/Contents/Resources/GameReaderHelper",
        Path.home() / "Applications/GameReader.app/Contents/MacOS/GameReaderHelper",
        Path.home() / "Applications/GameReader.app/Contents/MacOS/GameReader",
        Path("/Users/kamil/gamer/macos/GameReader"),
    ]
    for path in candidates:
        if path is not None and path.is_file() and os.access(path, os.X_OK):
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
        line, sock, rest = helper_call("TAP", timeout=16)
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
    """Piper (szybki) + opcjonalnie Chatterbox Multilingual (emocje, wolniejszy)."""

    REF_TEXT = (
        "Dzień dobry. Jestem lektorem gry. Czytam polskie napisy spokojnie i wyraźnie, "
        "żebyś zawsze rozumiał dialog. Uwaga, ruszaj szybko. Przepraszam. Kocham cię. "
        "Ciemność nadchodzi, ale damy radę."
    )

    def __init__(self):
        self.player = None
        self.lock = threading.Lock()
        self.synth_lock = threading.Lock()
        self.backend = None
        self.preferred = "piper"
        self.model = None
        self.voice = None
        self.device = "cpu"
        self.tts_scale = 1.0
        self.ffmpeg = which_bin("ffmpeg")
        self._load_error = ""

    def ensure(self):
        if self.backend is not None:
            return
        order = ["piper", "chatterbox"] if self.preferred == "piper" else ["chatterbox", "piper"]
        errors = []
        for name in order:
            try:
                if name == "chatterbox":
                    self._ensure_chatterbox()
                else:
                    self._ensure_piper()
                return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self.backend = None
                self.model = None
                self.voice = None
        self._load_error = " | ".join(errors)
        raise RuntimeError(self._load_error or "Brak lektora.")

    def _ensure_piper(self):
        from piper import PiperVoice

        if not JARVIS_ONNX.is_file():
            raise RuntimeError("Brak lokalnego głosu lektora (Piper Jarvis).")
        self.voice = PiperVoice.load(str(JARVIS_ONNX))
        self.backend = "piper"

    def _ensure_chatterbox(self):
        import os

        import perth
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        os.environ["TQDM_DISABLE"] = "1"
        # resemble-perth 1.0.1 bywa bez Implicit — Chatterbox wtedy pada na NoneType
        if getattr(perth, "PerthImplicitWatermarker", None) is None:
            perth.PerthImplicitWatermarker = perth.DummyWatermarker

        if torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"
        self.model = ChatterboxMultilingualTTS.from_pretrained(self.device)
        self._ensure_ref_wav()
        self.model.prepare_conditionals(str(CHATTERBOX_REF), exaggeration=0.5)
        self.backend = "chatterbox"

    def _ensure_ref_wav(self):
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        if CHATTERBOX_REF.is_file() and CHATTERBOX_REF.stat().st_size > 8000:
            return
        from piper import PiperVoice, SynthesisConfig

        if not JARVIS_ONNX.is_file():
            raise RuntimeError("Brak Piper Jarvis do zbudowania próbki głosu Chatterbox.")
        voice = PiperVoice.load(str(JARVIS_ONNX))
        cfg = SynthesisConfig(length_scale=1.0, volume=1.0, noise_w_scale=0.5)
        with wave.open(str(CHATTERBOX_REF), "wb") as handle:
            voice.synthesize_wav(self.REF_TEXT, handle, syn_config=cfg)
        if not CHATTERBOX_REF.is_file() or CHATTERBOX_REF.stat().st_size < 8000:
            raise RuntimeError("Nie udało się zbudować próbki głosu Chatterbox.")

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

    def prepare(self, text, source=None, volume=1.0):
        self.ensure()
        mood = detect_mood(text, source)
        intensity = max(0.0, min(1.5, float(volume)))
        if self.backend == "chatterbox":
            profile = MOODS[mood]
            exag = float(profile.get("exag", 0.5))
            exag = max(0.15, min(1.0, exag * (0.65 + 0.55 * intensity)))
            cfg = float(profile.get("cfg", 0.5))
            cfg = max(0.15, min(0.7, cfg))
            key = f"cb|{text}|{mood}|{exag:.2f}|{cfg:.2f}|{self.tts_scale:.2f}"
            path = CACHE_DIR / f"{text_key(key)}.wav"
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.stat().st_size < 64:
                self._synth_chatterbox(text, path, exag, cfg, intensity)
            return mood, path
        voice = mood_voice(mood, intensity=intensity, tts_scale=self.tts_scale)
        # cache zależny od segmentów emocji + intensity
        seg_key = "|".join(f"{m}:{t}" for t, m in split_emotion_segments(text))
        key = f"px|{text}|{seg_key}|{intensity:.2f}|{self.tts_scale:.2f}"
        path = CACHE_DIR / f"{text_key(key)}.wav"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size < 64:
            self._synth_piper_expressive(text, path, intensity)
        return mood, path

    def speak(self, text, source=None, volume=1.0):
        mood, path = self.prepare(text, source=source, volume=volume)
        self.stop()
        self._play_file(path)
        return mood

    def _synth_chatterbox(self, text, path, exaggeration, cfg_weight, volume):
        import contextlib
        import os

        import torch
        import torch.nn.functional as F
        from chatterbox.mtl_tts import drop_invalid_tokens, punc_norm
        from chatterbox.models.t3.modules.cond_enc import T3Cond

        os.environ["TQDM_DISABLE"] = "1"
        max_tokens = int(min(180, max(72, len(text) * 6)))

        with self.synth_lock:
            with open(os.devnull, "w") as devnull, contextlib.redirect_stderr(devnull), contextlib.redirect_stdout(devnull):
                if float(exaggeration) != float(self.model.conds.t3.emotion_adv[0, 0, 0].item()):
                    cond = self.model.conds.t3
                    self.model.conds.t3 = T3Cond(
                        speaker_emb=cond.speaker_emb,
                        cond_prompt_speech_tokens=cond.cond_prompt_speech_tokens,
                        emotion_adv=exaggeration * torch.ones(1, 1, 1),
                    ).to(device=self.model.device)
                normed = punc_norm(text)
                text_tokens = self.model.tokenizer.text_to_tokens(normed, language_id="pl").to(self.model.device)
                text_tokens = torch.cat([text_tokens, text_tokens], dim=0)
                sot = self.model.t3.hp.start_text_token
                eot = self.model.t3.hp.stop_text_token
                text_tokens = F.pad(text_tokens, (1, 0), value=sot)
                text_tokens = F.pad(text_tokens, (0, 1), value=eot)
                with torch.inference_mode():
                    speech_tokens = self.model.t3.inference(
                        t3_cond=self.model.conds.t3,
                        text_tokens=text_tokens,
                        max_new_tokens=max_tokens,
                        temperature=0.7,
                        cfg_weight=float(cfg_weight),
                        repetition_penalty=2.0,
                        min_p=0.05,
                        top_p=1.0,
                    )
                    speech_tokens = drop_invalid_tokens(speech_tokens[0]).to(self.model.device)
                    wav, _ = self.model.s3gen.inference(
                        speech_tokens=speech_tokens,
                        ref_dict=self.model.conds.gen,
                    )
                    wav = wav.squeeze(0).detach().cpu()
            if wav is None:
                raise RuntimeError("Chatterbox nic nie wygenerował.")
            if wav.ndim == 1:
                wav = wav.unsqueeze(0)
            gain = max(0.45, min(1.35, float(volume)))
            audio = (wav.float() * gain).clamp(-1.0, 1.0).numpy()
            if audio.ndim == 2:
                audio = audio[0]
            sr = int(getattr(self.model, "sr", 24000) or 24000)
            tmp = Path(str(path) + ".part")
            pcm = (audio * 32767.0).astype("<i2")
            with wave.open(str(tmp), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sr)
                handle.writeframes(pcm.tobytes())
            tmp.replace(path)
        if not path.exists() or path.stat().st_size < 64:
            raise RuntimeError("Nie udało się zsyntetyzować Chatterbox.")

    def _synth_piper_chunk(self, text, length, volume, noise, noise_scale):
        from piper import SynthesisConfig

        cfg = SynthesisConfig(
            length_scale=length,
            volume=volume,
            noise_w_scale=noise,
            noise_scale=noise_scale,
        )
        buf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        buf.close()
        try:
            with wave.open(buf.name, "wb") as handle:
                self.voice.synthesize_wav(text, handle, syn_config=cfg)
            with wave.open(buf.name, "rb") as handle:
                sr = handle.getframerate()
                sw = handle.getsampwidth()
                ch = handle.getnchannels()
                frames = handle.readframes(handle.getnframes())
            return sr, sw, ch, frames
        finally:
            try:
                os.unlink(buf.name)
            except OSError:
                pass

    def _synth_piper_expressive(self, text, path, intensity):
        """Każdy fragment po ! ? … dostaje osobne tempo/głośność + pauzę."""
        segments = split_emotion_segments(text)
        if not segments:
            raise RuntimeError("Brak tekstu do syntezy.")
        chunks = []
        sr = sw = ch = None
        for idx, (piece, mood) in enumerate(segments):
            voice = mood_voice(mood, intensity=intensity, tts_scale=self.tts_scale)
            # zostaw interpunkcję w tekście — Piper lepiej trzyma intonację pytania/wykrzyknienia
            csr, csw, cch, frames = self._synth_piper_chunk(
                piece,
                voice["length"],
                voice["volume"],
                voice["noise"],
                voice["noise_scale"],
            )
            if sr is None:
                sr, sw, ch = csr, csw, cch
            chunks.append(frames)
            if idx + 1 < len(segments):
                pause = float(voice["pause"])
                # przecinek / wielokropek = dłuższa cisza
                if mood == "soft":
                    pause = max(pause, 0.20)
                elif piece.rstrip().endswith((",", ";", "—")):
                    pause = max(pause, 0.10)
                n = int(sr * pause)
                chunks.append(b"\x00" * (n * sw * ch))
        tmp = Path(str(path) + ".part")
        with wave.open(str(tmp), "wb") as handle:
            handle.setnchannels(ch)
            handle.setsampwidth(sw)
            handle.setframerate(sr)
            for block in chunks:
                handle.writeframes(block)
        tmp.replace(path)
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
        # kara za brak diakrytyków przy długim polskim tekście
        if len(raw) >= 12 and sum(ch in PL_MARK for ch in raw + fixed) == 0 and looks_polish(fixed):
            score -= 18
        if self.skip_yellow_speaker and looks_like_speaker_name(raw):
            score -= 30
        return score

    def read(self, frame):
        if frame is None or frame.size == 0:
            return ""
        from PIL import ImageEnhance, ImageOps

        rgb = frame[:, :, ::-1] if frame.shape[-1] == 3 else frame
        image = Image.fromarray(np.ascontiguousarray(rgb)).convert("RGB")
        width, height = image.size
        if 8 <= height < 120:
            scale = 120 / max(height, 1)
            image = image.resize((max(8, int(width * scale)), int(height * scale)), Image.Resampling.LANCZOS)
            rgb = np.array(image)
        langs = list(self.languages or ["pl-PL"])
        if "pl-PL" not in langs:
            langs = ["pl-PL"] + langs
        contrast = ImageOps.autocontrast(ImageEnhance.Contrast(image.convert("L")).enhance(self.boost)).convert("RGB")
        auto = ImageOps.autocontrast(image).convert("RGB")
        # wyłącznie accurate — fast na macOS gubi ąęćłńóśźż
        attempts = [
            (image, langs),
            (auto, langs),
            (contrast, langs),
        ]
        best = ""
        best_raw = ""
        best_score = -1
        for candidate, lang_pref in attempts:
            try:
                text = self._run(candidate, "vision", "accurate", lang_pref, rgb=rgb if candidate is image else None)
            except Exception:
                continue
            if self.skip_yellow_speaker:
                text = strip_speaker_label(text)
            score = self._score(text)
            if score > best_score:
                best_score = score
                best_raw = text
                best = repair_polish_ocr(text)
                # wystarczy wynik z polskimi znakami
                if score >= 14 and sum(ch in PL_MARK for ch in best) >= 1:
                    break
                if score >= 22:
                    break
        if best_raw and (not usable_ocr(best) or len(best) + 8 < len(re.sub(r"\s+", "", best_raw))):
            best = repair_polish_ocr(best_raw) or normalize_text(best_raw)
        if self.skip_yellow_speaker:
            best = strip_speaker_label(best)
        return best if usable_ocr(best) else ""

class Engine:
    def __init__(self, emit):
        self.emit = emit
        self.cfg = load_config()
        self.region = tuple(self.cfg["region"]) if self.cfg.get("region") else None
        self.mode = normalize_mode(self.cfg.get("mode", "auto"))
        self.device = self.cfg.get("device") or PS_REMOTE
        self.overlay = False
        self.auto_interval = bool(self.cfg.get("autoInterval", True))
        self.interval = max(MIN_INTERVAL, min(0.8, float(self.cfg.get("interval", DEFAULT_INTERVAL))))
        self.intensity = float(self.cfg.get("intensity", 1.0))
        self.running = False
        self.last_key = ""
        self.last_subtitle = ""
        self.subtitle_until = 0.0
        self.speaking_text = ""
        self._ocr_candidate = ""
        self._ocr_candidate_n = 0
        self._spoken_folds = {}
        self.confirm_frames = OCR_CONFIRM_FRAMES
        self.speak_cooldown = 5.5
        self.no_barge_in = True
        self.pending = None
        self.pending_lock = threading.Lock()
        self.has_pending = threading.Event()
        self._tts_interrupt = threading.Event()
        self.game = normalize_game(self.cfg.get("game", "rdr2"))
        self.game_regions = dict(self.cfg.get("games") or {})
        self.lock_region = bool(self.cfg.get("lockRegion", False))
        self.show_region = bool(self.cfg.get("showRegion", False))
        self.dock_corner = str(self.cfg.get("dockCorner") or "tr")
        self.collapsed = bool(self.cfg.get("collapsed", False))
        if self.dock_corner not in ("tl", "tr", "bl", "br"):
            self.dock_corner = "tr"
        self._last_win_sync = 0.0
        self.ps_window = None
        self.ocr = AppleVisionOcr()
        self.lektor = MaleLektor()
        # piper = szybki lektor do napisów; chatterbox opcjonalnie (wolniejszy, więcej emocji)
        preferred = str(self.cfg.get("ttsEngine") or "piper").strip().lower()
        if preferred in ("chatterbox", "piper"):
            self.lektor.preferred = preferred
        self.translator = ArgosTranslator()
        self.stt = ParakeetSTT()
        self.line_q = queue.Queue()
        self.transcriber = None
        self.devices = [PS_REMOTE] + [name for _i, name in list_input_devices()]
        self.apply_game(self.game, persist=False, announce=False, reset_lock=False)
        threading.Thread(target=self._tts_loop, daemon=True).start()
        threading.Thread(target=self._warmup_voice, daemon=True).start()

    def _warmup_voice(self):
        engine = getattr(self.lektor, "preferred", "piper")
        label = "Piper (szybki)" if engine == "piper" else "Chatterbox (emocje)"
        self.emit({"event": "status", "text": f"Ładuję lektora {label}…"})
        try:
            self.lektor.ensure()
            if self.lektor.backend == "chatterbox":
                self.emit({"event": "status", "text": f"Lektor Chatterbox gotowy ({self.lektor.device})."})
            else:
                self.emit({"event": "status", "text": "Lektor Piper Jarvis gotowy."})
        except Exception as exc:
            self.emit({"event": "status", "text": f"Lektor nie wstaje: {exc}"})

    def snapshot(self):
        return {
            "region": list(self.region) if self.region else None,
            "mode": self.mode,
            "device": self.device,
            "overlay": self.overlay,
            "interval": self.interval,
            "autoInterval": self.auto_interval,
            "intensity": self.intensity,
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
        }

    def persist(self):
        save_config(
            {
                "region": list(self.region) if self.region else None,
                "overlay": self.overlay,
                "interval": self.interval,
                "autoInterval": self.auto_interval,
                "intensity": self.intensity,
                "mode": self.mode,
                "device": self.device,
                "game": self.game,
                "games": self.game_regions,
                "lockRegion": self.lock_region,
                "showRegion": self.show_region,
                "dockCorner": self.dock_corner,
                "collapsed": self.collapsed,
                "ttsEngine": getattr(self.lektor, "preferred", "piper"),
            }
        )

    def save_settings(self):
        # Zapisz = utrwal aktualny pasek jako ręczny (nie nadpisuj go automatem okna)
        if self.region:
            self.lock_region = True
            self.game_regions[self.game] = {"region": [int(v) for v in self.region]}
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
        if "intensity" in data:
            self.intensity = float(data["intensity"])
        if "game" in data:
            self.apply_game(data["game"], persist=False, announce=True, reset_lock=True)
        if "autoInterval" in data:
            self.auto_interval = bool(data["autoInterval"])
            if self.auto_interval:
                self.interval = float(GAME_PROFILES[self.game]["interval"])
        if "interval" in data:
            self.auto_interval = False
            self.interval = max(MIN_INTERVAL, min(0.8, float(data["interval"])))
        if "showRegion" in data:
            self.show_region = bool(data["showRegion"])
        if "dockCorner" in data and data["dockCorner"] in ("tl", "tr", "bl", "br"):
            self.dock_corner = data["dockCorner"]
        if "lockRegion" in data:
            self.lock_region = bool(data["lockRegion"])
        if "collapsed" in data:
            self.collapsed = bool(data["collapsed"])
        self.persist()
        self.emit({"event": "state", **self.snapshot()})

    def apply_game(self, game, persist=True, announce=True, reset_lock=True):
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
        if reset_lock:
            self.lock_region = False
            saved = self.game_regions.get(self.game) or {}
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
                self.emit({"event": "status", "text": f"{profile['label']}: mam okno PS Remote Play {width}×{height}."})
            else:
                self.emit({"event": "status", "text": f"{profile['label']}: nie widzę okna PS Remote Play. Odpal Remote Play."})

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
        win = find_remote_play_window()
        self.ps_window = win
        if self.lock_region and self.region:
            # Ręcznie zapisany pasek zostaje — nie kasuj go przez overlap/automatu.
            if win is None or self._region_overlap(self.region, win) >= 0.12:
                return True
            # okno się przesunęło: dociągnij pasek do dolnego pasa, zachowaj lock
            band = window_subtitle_band(win, GAME_PROFILES[self.game])
            # jeśli stary pasek był wyżej/niżej, zachowaj względną wysokość w oknie
            rx, ry, rw, rh = [int(v) for v in self.region]
            _wx, wy, _ww, wh = [int(v) for v in win]
            rel = (ry - wy) / max(wh, 1)
            if 0.35 <= rel <= 0.95:
                new_top = int(wy + rel * wh)
                self.region = (band[0], max(wy, min(new_top, wy + wh - rh)), band[2], rh)
            else:
                self.region = band
            self.game_regions[self.game] = {"region": list(self.region)}
            self.emit({"event": "state", **self.snapshot()})
            return True
        if win is None:
            return self.region is not None
        band = window_subtitle_band(win, GAME_PROFILES[self.game])
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
            self.game_regions[self.game] = {"region": list(region)}
        self.persist()
        if region is None:
            self.emit({"event": "status", "text": "Nie zaznaczono obszaru."})
            self.emit({"event": "state", **self.snapshot()})
            return
        self.emit({"event": "state", **self.snapshot()})
        self._preview_and_ocr(speak=False)

    def test(self):
        if not self._sync_remote_band(force=True) and self.region is None:
            self.emit({"event": "status", "text": "Nie widzę PS Remote Play. Odpal grę albo zaznacz pasek."})
            return
        self.persist()
        self.emit({"event": "status", "text": "Robię test napisów…"})
        self._preview_and_ocr(speak=True)

    def start(self):
        if self.running:
            return
        self.running = True
        self.last_key = ""
        self.last_subtitle = ""
        self.subtitle_until = 0.0
        self.speaking_text = ""
        self._ocr_candidate = ""
        self._ocr_candidate_n = 0
        self._spoken_folds = {}
        self._tts_interrupt.clear()
        self._flush_line_q()
        self.emit({"event": "running", "on": True})
        if self.mode == "audio":
            self.emit({"event": "status", "text": "Podpinam dźwięk PS Remote Play…"})
            target = self._audio_loop
        elif self.mode == "ocr":
            if not self._sync_remote_band(force=True) and not self.lock_region:
                self.running = False
                self.emit({"event": "running", "on": False})
                self.emit({"event": "status", "text": "Nie widzę PS Remote Play. Odpal Remote Play albo zaznacz pasek."})
                return
            self.emit({"event": "status", "text": "Tylko napisy — pasek z okna PS Remote Play."})
            target = self._ocr_only_loop
        elif not self._sync_remote_band(force=True) and not self.lock_region:
            self.emit({"event": "status", "text": "Brak okna PS Remote Play — czytam z dźwięku."})
            target = self._audio_loop
        else:
            self.emit({"event": "status", "text": "Napisy z okna PS Remote Play. Dźwięk EN→PL w tle."})
            target = self._hybrid_loop
        threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self.running = False
        self._flush_line_q()
        self.speaking_text = ""
        if self.transcriber is not None:
            self.transcriber.stop()
            self.transcriber = None
        self.lektor.stop()
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

    def _capture_region(self):
        if self.region is None:
            raise RuntimeError("Brak obszaru.")
        left, top, width, height = [int(v) for v in self.region]
        if width < 8 or height < 8:
            raise RuntimeError("za mały obszar")
        frame = capture_remote_play_band(left, top, width, height)
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

    def _recently_spoken(self, text):
        fold = polish_fold(text or "")
        if not fold:
            return False
        self._prune_spoken()
        now = time.monotonic()
        for key, exp in self._spoken_folds.items():
            if now >= exp:
                continue
            if key == fold:
                return True
            short, long = (key, fold) if len(key) <= len(fold) else (fold, key)
            if len(short) >= 8 and short in long and len(short) / max(len(long), 1) >= 0.72:
                return True
        return False

    def _mark_spoken(self, text):
        fold = polish_fold(text or "")
        if not fold:
            return
        self._prune_spoken()
        self._spoken_folds[fold] = time.monotonic() + float(self.speak_cooldown or 5.5)

    def _offer_line(self, src, translate):
        src = normalize_text(src)
        if not src:
            return
        if same_utterance(src, self.speaking_text):
            return
        if self._recently_spoken(src):
            return
        # w trakcie czytania: nie ucinaj — najwyżej zapamiętaj nowszą kwestię na potem
        if self.speaking_text:
            if extends_utterance(self.speaking_text, src):
                self.last_subtitle = src
                return
            with self.pending_lock:
                if self.pending and same_utterance(src, self.pending[0]):
                    return
                # nowa kwestia — nie przerywaj bieżącej (Hogwarts OCR miga jak szalony)
                if self.no_barge_in:
                    self.pending = (src, translate)
                    self.has_pending.set()
                    return
            with self.pending_lock:
                self.pending = (src, translate)
                self.has_pending.set()
            self._tts_interrupt.set()
            self.lektor.stop()
            return
        with self.pending_lock:
            if self.pending and same_utterance(src, self.pending[0]):
                return
            if self.pending and extends_utterance(self.pending[0], src):
                self.pending = (src, translate)
                self.has_pending.set()
                return
            self.pending = (src, translate)
            self.has_pending.set()

    def _take_pending(self):
        with self.pending_lock:
            item = self.pending
            self.pending = None
            self.has_pending.clear()
        return item

    def _tts_loop(self):
        while True:
            if not self.has_pending.wait(timeout=0.15):
                continue
            item = self._take_pending()
            if item is None:
                continue
            src, translate = item
            if same_utterance(src, self.speaking_text) or self._recently_spoken(src):
                continue
            try:
                text = self.translator.translate(src) if translate else src
                text = normalize_text(text)
                if not text:
                    continue
                source = src if translate else None
                mood, path = self.lektor.prepare(text, source=source, volume=self.intensity)
                with self.pending_lock:
                    newer = self.pending
                # przy no_barge_in dokończ obecną syntezę; nowszy zostanie na kolejkę
                if newer and not same_utterance(newer[0], src) and not self.no_barge_in:
                    continue
                self._tts_interrupt.clear()
                self.speaking_text = src
                self.last_key = text_key(src)
                self.last_subtitle = src
                self.lektor._play_file(path)
                self.emit({"event": "line", "text": text, "mood": mood, "label": MOODS[mood]["label"]})
                if self.no_barge_in:
                    self.lektor.wait()
                else:
                    self.lektor.wait(
                        interrupt_check=lambda: self._tts_interrupt.is_set()
                        or (
                            self.pending is not None
                            and not same_utterance(self.pending[0] if self.pending else "", src)
                        )
                    )
                self._mark_spoken(src)
            except Exception as exc:
                self.emit({"event": "status", "text": f"Błąd głosu: {exc}"})
            finally:
                self.speaking_text = ""
                self._tts_interrupt.clear()

    def _on_draft(self, text):
        self.emit({"event": "heard", "text": text})

    def _on_heard(self, heard):
        if not heard or heard.lower() in JUNK_HEARD:
            return
        self.emit({"event": "heard", "text": heard})
        if self.speaking_text or time.monotonic() < self.subtitle_until:
            return
        if text_key(heard) == self.last_key or self._recently_spoken(heard):
            return
        self._offer_line(heard, True)

    def _flush_line_q(self):
        try:
            while True:
                self.line_q.get_nowait()
        except queue.Empty:
            pass
        with self.pending_lock:
            self.pending = None
            self.has_pending.clear()
        self._tts_interrupt.set()
        self.lektor.stop()
        self._tts_interrupt.clear()

    def _on_subtitle(self, src):
        if not usable_ocr(src):
            return
        if self._recently_spoken(src):
            self.last_subtitle = src
            return
        # OCR dogląda reszty — nie restartuj lektora w środku
        if extends_utterance(self.speaking_text, src):
            self.last_subtitle = src
            self._ocr_candidate = ""
            self._ocr_candidate_n = 0
            return
        if extends_utterance(self.last_subtitle, src):
            self.last_subtitle = src
            self.last_key = text_key(src)
            self.subtitle_until = time.monotonic() + 2.0
            # jeśli nic nie czytamy, dopisz pełniejszą wersję
            if not self.speaking_text:
                self._offer_line(src, False)
            return
        if same_utterance(src, self.speaking_text) or same_utterance(src, self.last_subtitle):
            self._ocr_candidate = ""
            self._ocr_candidate_n = 0
            return
        need = max(1, int(getattr(self, "confirm_frames", OCR_CONFIRM_FRAMES)))
        if not same_utterance(src, self._ocr_candidate):
            self._ocr_candidate = src
            self._ocr_candidate_n = 1
            if need > 1:
                return
        else:
            self._ocr_candidate_n += 1
            if self._ocr_candidate_n < need:
                return
        self._ocr_candidate = ""
        self._ocr_candidate_n = 0
        self.last_subtitle = src
        self.last_key = text_key(src)
        self.subtitle_until = time.monotonic() + 2.5
        mood = detect_mood(src)
        self.emit({"event": "status", "text": f"Napisy PL · {MOODS[mood]['label']}."})
        self._offer_line(src, False)

    def _ocr_only_loop(self):
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
                frame = self._capture_region()
                last_err = ""
                digest = hashlib.sha1(frame.tobytes()).hexdigest()[:20]
                if digest != last_hash or not self.last_subtitle:
                    last_hash = digest
                    src = self.ocr.read(frame)
                    if src:
                        empty_streak = 0
                        self._on_subtitle(src)
                    else:
                        empty_streak += 1
                        # nie czyść last_subtitle zbyt szybko — inaczej ta sama kwestia leci w kółko
                        if empty_streak >= 18 and not self.speaking_text:
                            self.last_subtitle = ""
                        if empty_streak >= 40 and self.lock_region and self.game not in self.game_regions:
                            self.lock_region = False
                            self.persist()
                            self._sync_remote_band(force=True)
                            empty_streak = 0
                            last_hash = ""
                            self.emit({"event": "status", "text": "Ręczny pasek nic nie widzi — wracam do automatu."})
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

    def _audio_loop(self):
        self.emit({"event": "status", "text": "Podpinam dźwięk w tle…"})
        self.transcriber = LiveTranscriber(
            self.stt,
            self._on_heard,
            on_draft=self._on_draft,
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

    def _audio_loop_ps_remote(self):
        tap = GameAudioTap()
        live = self.transcriber
        try:
            tap.start()
            heard_from = tap.message.replace("LISTENING ", "") if tap.message.startswith("LISTENING") else "PS Remote Play"
            self.emit({"event": "status", "text": f"Słucham {heard_from} · EN→PL, napisy PL bez tłumaczenia."})
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
            self.emit({"event": "perm", "text": str(exc)})
            self.running = False
        except Exception as exc:
            text = str(exc)
            if any(word in text.lower() for word in ("tcc", "zgody", "przechwytywania", "denied", "not permitted")):
                self.emit({"event": "perm", "text": text})
            else:
                self.emit({"event": "status", "text": f"Błąd PS Remote: {exc}"})
            self.running = False
        finally:
            tap.stop()

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

