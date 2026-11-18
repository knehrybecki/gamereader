# Głosy Jasona i Lucii — plan wdrożenia

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Przełącznik „Głosy Jasona i Lucii”: kwestie rozpoznane jako Jasona/Lucii czyta klon z gotowej próbki, resztę lektor.

**Architecture:** `HeroBank` dziedziczy po `SpeakerBank` (te same wątki, oś czasu wypowiedzi, `observe`/`voice_for`), ale zamiast odkrywać postacie ma dwie zapisane z góry: wektor mówcy i głos z `GameReader/heroes/<imię>/sample.wav`. Imię z dwukropkiem w napisie ma pierwszeństwo przed barwą. Wybór głosu kwestii wyciągnięty do `Engine._pick_voice`.

**Tech Stack:** Python 3.14 (silnik), proces OmniVoice (VoiceStudio venv), Electron (przełącznik), unittest.

**Spec:** `docs/superpowers/specs/2026-11-18-glosy-jasona-i-lucii-design.md`

## Global Constraints

- Domyślnie wyłączone: cfg `"heroVoices": false`.
- Działa tylko z lektorem VoiceStudio (`self.lektor.cloud` gotowy); bez niego zawsze lektor.
- Próg barwy `HERO_MIN_SIM = 0.45`; każda decyzja (bohater/lektor, podobieństwo) w `log_timing`.
- Nigdy cisza: brak pewności = lektor (bez trybu „cloneOnly”).
- Przy polskich napisach bez Parakeeta: `HeroBank` nie transkrybuje.
- Próbki bohaterów nie trafiają do repo ani do paczki aplikacji.

## Review Focus

- Brak folderu `heroes/` albo próbki jednego bohatera → drugi działa, brak obu = zwykły lektor, bez błędu (test w Task 1).
- Restart procesu VoiceStudio (zmiana voicepacka) → głosy bohaterów rejestrują się od nowa w nowym procesie (test w Task 1: zmiana `cloud` → ponowne zapisanie).
- Imię spoza bohaterów („LUCA: …”) → lektor, nie najbliższy bohater (test w Task 2).
- Włączone jednocześnie „Klonuj głosy postaci” i „Głosy Jasona i Lucii” → wygrywa tryb bohaterów (test w Task 3).
- Kwestia z dwóch części, z których jedna ma imię → imię decyduje (test w Task 3).

---

### Task 1: HeroBank — zapisane postacie i rozpoznanie po barwie

**Files:**
- Modify: `gamereader_engine.py` (po klasie `SpeakerBank`; `OmniVoiceLocal` obok `set_voice`)
- Test: `ci/test_hero_voices.py` (nowy)

**Interfaces:**
- Produces: `HEROES_DIR = CONFIG_PATH.parent / "heroes"`, `HERO_MIN_SIM = 0.45`, `HERO_WAIT_SEC = 0.8`
- Produces: `class HeroBank(SpeakerBank)`: `__init__(cloud_getter, log=None, heroes_dir=None)`, `key_for_name(name: str) -> str | None` (klucz tylko gdy bohater gotowy), klucze `"hero_jason"`, `"hero_lucia"`
- Produces: `OmniVoiceLocal.set_voice_file(key, wav_path, text) -> float | None` (op `voice` z plikiem, bez zapisu 16 kHz; podbija `voice_rev` jak `set_voice`), `OmniVoiceLocal.embed_file(wav_path) -> np.ndarray | None`

- [ ] **Step 1: Testy** (fałszywy `cloud` z `ready=True`, `embed(audio)` i `embed_file(path)` zwracające wektory zależne od treści, `set_voice_file` zapisujące wywołania; tymczasowy `heroes_dir` z `jason/` i `lucia/`):
  - `test_heroes_enrolled_from_samples`: po pierwszym `_handle` w `bank.speakers` są `hero_jason` i `hero_lucia`, `ready=True`; `set_voice_file` wywołane z plikiem `sample.wav` i tekstem `sample.txt`.
  - `test_close_voice_gets_hero_far_voice_gets_none`: `_assign(vec_jasona, 3.0, create=True)` → `("hero_jason", ≥0.45)`; wektor obcy (cos 0.2) → `(None, …)`; `len(bank.speakers) == 2` (nic nowego).
  - `test_missing_hero_folder_is_fine`: tylko `jason/` → działa Jason; pusty katalog → `any_ready()` False, bez wyjątku.
  - `test_new_voicestudio_process_reenrolls`: zmiana obiektu `cloud` → `set_voice_file` wywołane ponownie dla nowego.
  - `test_no_transcription_and_no_sample_collection`: `_collect` nie jest wołane, transkrypcja nie istnieje (konstruktor bez `transcribe`).
- [ ] **Step 2:** `gr/bin/python -m unittest ci.test_hero_voices` → FAIL (brak `HeroBank`).
- [ ] **Step 3:** Implementacja: zapis bohaterów leniwie w wątku banku (`_handle`) przy pierwszym/nowym `cloud`; `reset()` czyści też znacznik zapisu; `_assign` bez tworzenia postaci — najbliższy bohater przy `sim ≥ HERO_MIN_SIM`, log `bohaterowie: … podobieństwo 0.xx → Jason/lektor`; `_collect` = nic. `set_voice_file`/`embed_file` w `OmniVoiceLocal` na istniejących opach `voice`/`embed`.
- [ ] **Step 4:** testy PASS + cały `ci/`.
- [ ] **Step 5:** Commit.

### Task 2: Imię z dwukropkiem jako sygnał

**Files:**
- Modify: `gamereader_engine.py` (`strip_colon_speaker`, nowe `colon_speaker`)
- Test: `ci/test_hero_voices.py`

**Interfaces:**
- Consumes: `HeroBank.key_for_name` (Task 1)
- Produces: `colon_speaker(text: str) -> str | None` — imię wycięte przez `strip_colon_speaker` z napisu, który dał `text` (rejestr ostatnich 64)

- [ ] **Step 1: Testy:** `strip_colon_speaker("JASON: Wsiadaj do auta.")` → `"Wsiadaj do auta."` i `colon_speaker("Wsiadaj do auta.") == "JASON"`; `colon_speaker("Coś innego.") is None`; `key_for_name("Jason")`/`"LUCIA"` → klucze; `key_for_name("LUCA") is None`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Rejestr `OrderedDict` w module, zapisywany w `strip_colon_speaker` przy wycięciu; `key_for_name` porównuje bez wielkości liter i bez polskich znaków (`polish_fold`).
- [ ] **Step 4:** PASS + cały `ci/`.
- [ ] **Step 5:** Commit.

### Task 3: Wybór głosu kwestii i przełącznik

**Files:**
- Modify: `gamereader_engine.py` (`Engine.__init__`, `configure`, `snapshot`, `persist`, blok „kto mówi” w pętli lektora → `Engine._pick_voice`)
- Modify: `electron/renderer/index.html`, `electron/renderer/app.js` (przełącznik jak `cloneVoices`)
- Test: `ci/test_hero_voices.py`

**Interfaces:**
- Consumes: `HeroBank`, `colon_speaker`, `HERO_WAIT_SEC`
- Produces: `Engine._pick_voice(parts: list[str], src: str, translate: bool) -> tuple[str | None, bool]` — (klucz głosu albo None, czy pominąć kwestię); w trybie bohaterów drugie zawsze False

- [ ] **Step 1: Testy** (Engine przez `__new__`, fałszywy bank/cloud):
  - `test_name_wins_over_voice`: część z imieniem „JASON” → `("hero_jason", False)` bez wołania `voice_for`.
  - `test_voice_match_used_without_name`: `voice_for` zwraca `hero_lucia` → `("hero_lucia", False)`; wołane z `wait=HERO_WAIT_SEC`, `text=None` przy polskich napisach.
  - `test_unsure_means_lektor_never_silence`: `voice_for` → None → `(None, False)`.
  - `test_hero_mode_wins_over_clone_mode`: cfg `cloneVoices` i `heroVoices` → `isinstance(engine.speakers, HeroBank)`.
  - `test_old_clone_mode_unchanged`: `SpeakerBank` + brak głosu + `cloneOnly` → `(None, True)`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implementacja; przełącznik `heroVoices` (etykieta „Głosy Jasona i Lucii”, podpowiedź: „Kwestie Jasona i Lucii ich głosami z próbek zwiastuna. Reszta i każda niepewność — lektor.”), widoczny przy lektorze VoiceStudio.
- [ ] **Step 4:** PASS + cały `ci/`; `node --check electron/renderer/app.js`.
- [ ] **Step 5:** Commit.

### Task 4: Próbki na miejscu i próba na YouTube

- [ ] Skopiować `scratchpad/jason/sample.{wav,txt}` → `~/Library/Application Support/GameReader/heroes/jason/`, Lucia gdy gotowa.
- [ ] Na zwiastunie GTA VI w Chrome (YouTube) z włączonym przełącznikiem: z `/tmp/livedub-engine.log` zebrać linie `bohaterowie:` i porównać z tym, kto naprawdę mówi; stroić `HERO_MIN_SIM` (wspólnie z użytkownikiem).
