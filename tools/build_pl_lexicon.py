"""Buduje pl_lex.sqlite — słownik do poprawiania polskich odczytów PaddleOCR (gamereader_engine.latin_ocr_polish).

Źródła (CC BY-SA 4.0 / OPUS, oba z napisów filmowych OpenSubtitles 2018):
  pl_full.txt  https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/pl/pl_full.txt
  pl.txt.gz    https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/mono/pl.txt.gz

Tabele:
  w(w, f, n)   słowo, forma bez ogonków, liczba wystąpień (≥ MIN_COUNT) — wybór „całe” zamiast „cate”
  ctx(k, form) sąsiad rozstrzyga, którą formę wybrać, gdy obie są poprawne: „L|z|dzisiejsza” → „dzisiejszą”
               (k = „L|poprzednie|forma bez ogonków” albo „R|forma bez ogonków|następne”)

Użycie: python tools/build_pl_lexicon.py count pl_full.txt pl.txt.gz ctx_counts.tsv [liczba zdań]
        python tools/build_pl_lexicon.py build pl_full.txt ctx_counts.tsv pl_lex.sqlite
"""
import gzip
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from collections import Counter, defaultdict

MIN_COUNT = 10  # rzadsze słowa to w napisach głównie literówki
GROUP_MIN = 20  # forma z ogonkami bierze udział w wyborze z kontekstu, jeśli występuje choć tyle razy
CTX_MIN = 6  # tyle razy para słów musi wystąpić, żeby trafić do pliku z liczeniem
CTX_KEEP = 20  # a tyle, żeby trafić do słownika (pełny korpus: ~420 tys. kontekstów, ~15 MB)
FORM_SHARE = 0.9  # forma o takim udziale w grupie wygrywa i bez sąsiadów (silnik: OCR_FORM_SHARE)
CTX_SHARE = 0.85  # sąsiad rozstrzyga, gdy jedna forma ma co najmniej taki udział
WORD = re.compile(r"[a-ząćęłńóśźż]+")
FOLD = str.maketrans("ąćęłńóśźż", "acelnoszz")


def fold(word):
    return word.translate(FOLD)


def load_words(path):
    words = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            word, _sp, count = line.rstrip("\n").rpartition(" ")
            word = unicodedata.normalize("NFC", word)
            if count.isdigit() and int(count) >= MIN_COUNT and WORD.fullmatch(word):
                words[word] = words.get(word, 0) + int(count)
    return words


def ambiguous_groups(words):
    """Grupy form o tej samej postaci bez ogonków, z których co najmniej dwie są częste („woda”/„wodą”, „ze”/„że”)."""
    groups = defaultdict(list)
    for word, count in words.items():
        if count >= GROUP_MIN:
            groups[fold(word)].append(word)
    return {key: forms for key, forms in groups.items() if len(forms) >= 2}


def count_contexts(corpus, forms, limit):
    left, right = Counter(), Counter()
    started = time.time()
    with gzip.open(corpus, "rt", encoding="utf-8", errors="ignore") as handle:
        for number, line in enumerate(handle):
            if number >= limit:
                break
            tokens = WORD.findall(line.lower())
            for i, token in enumerate(tokens):
                if token in forms:
                    if i:
                        left[tokens[i - 1] + " " + token] += 1
                    if i + 1 < len(tokens):
                        right[token + " " + tokens[i + 1]] += 1
            if number and number % 5_000_000 == 0:
                print(f"{number:,} zdań, {time.time() - started:.0f}s, par {len(left) + len(right):,}", flush=True)
                if len(left) + len(right) > 60_000_000:  # pamięć: rzadkie pary i tak odpadną
                    for table in (left, right):
                        for key in [key for key, n in table.items() if n < 2]:
                            del table[key]
    return left, right


def save_counts(left, right, words, path):
    """Pary zebrane wokół formy bez ogonków: „L|z|dzisiejsza  dzisiejszą:40,dzisiejsza:2”."""
    by_key = defaultdict(Counter)
    for table, side in ((left, "L"), (right, "R")):
        for pair, n in table.items():
            first, second = pair.split(" ")
            if side == "L" and first in words:
                by_key[f"L|{first}|{fold(second)}"][second] += n
            elif side == "R" and second in words:
                by_key[f"R|{fold(first)}|{second}"][first] += n
    with open(path, "w", encoding="utf-8") as handle:
        for key, counts in by_key.items():
            if sum(counts.values()) >= CTX_MIN:
                handle.write(key + "\t" + ",".join(f"{form}:{n}" for form, n in counts.most_common()) + "\n")


def decisive(path, words):
    """Klucze kontekstu, w których jedna forma wyraźnie wygrywa („L|z|dzisiejsza” → „dzisiejszą”). Zapisane są też
    te potwierdzające formę bez ogonków — blokują zmianę, gdy druga strona mówi co innego. Pomijamy formy, które
    i tak wygrywają bez sąsiadów („sie” → „się”)."""
    group, top = defaultdict(int), {}
    for word, n in words.items():
        key = fold(word)
        group[key] += n
        if n > top.get(key, ("", 0))[1]:
            top[key] = (word, n)
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            key, _tab, packed = line.rstrip("\n").partition("\t")
            counts = [(form, int(n)) for form, _colon, n in (item.rpartition(":") for item in packed.split(","))]
            total = sum(n for _form, n in counts)
            form, best = counts[0]
            if total < CTX_KEEP or best < CTX_SHARE * total:
                continue
            leader, leader_n = top.get(fold(form), ("", 0))
            if leader == form and leader_n >= FORM_SHARE * group[fold(form)]:
                continue
            rows.append((key, form))
    return rows


def build(freq_path, counts_path, out):
    words = load_words(freq_path)
    rows = decisive(counts_path, words)
    Path(out).unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.executescript(
        "CREATE TABLE w(f TEXT NOT NULL, w TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (f, w)) WITHOUT ROWID;"
        "CREATE TABLE ctx(k TEXT PRIMARY KEY, form TEXT NOT NULL) WITHOUT ROWID;"
        "CREATE TABLE meta(source TEXT NOT NULL);"
    )
    db.execute("INSERT INTO meta VALUES (?)", (
        "Polskie napisy filmowe OpenSubtitles 2018: FrequencyWords (hermitdave, CC BY-SA 4.0) i korpus OPUS "
        "(opus.nlpl.eu, OpenSubtitles v2018 mono pl). Zbudowane przez tools/build_pl_lexicon.py.",))
    db.executemany("INSERT INTO w VALUES (?, ?, ?)", sorted((fold(w), w, n) for w, n in words.items()))
    db.executemany("INSERT INTO ctx VALUES (?, ?)", sorted(rows))
    db.commit()
    db.execute("VACUUM")
    db.close()
    with_marks = sum(form != fold(form) for key, form in rows)
    print(f"zapisano: słów {len(words):,}, kontekstów {len(rows):,} (z ogonkami: {with_marks:,})")


def count(freq_path, corpus, counts_path, limit):
    words = load_words(freq_path)
    groups = ambiguous_groups(words)
    forms = {form for group in groups.values() for form in group}
    print(f"słów {len(words):,}, grup niejednoznacznych {len(groups):,}, form {len(forms):,}", flush=True)
    left, right = count_contexts(corpus, forms, limit)
    save_counts(left, right, words, counts_path)


if __name__ == "__main__":
    if sys.argv[1] == "count":
        count(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]) if len(sys.argv) > 5 else 10**12)
    else:
        build(sys.argv[2], sys.argv[3], sys.argv[4])
