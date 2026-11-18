# Głosy Jasona i Lucii — projekt (krótki)

## Po co
Użytkownik gra w GTA VI z polskimi napisami i lektorem filmowym (VoiceStudio). Chce opcjonalnie słyszeć dwóch
głównych bohaterów ich własnymi głosami (klon z oficjalnego zwiastuna, z akcentem jak wyjdzie — bez dodatkowego
spolszczania, decyzja 18.11). Wyłączone = wszystko czyta lektor.

## Cele / nie-cele / anty-cele
- Cel: przełącznik „Głosy Jasona i Lucii”, domyślnie wyłączony, działa z lektorem VoiceStudio.
- Cel: kwestię Jasona/Lucii czyta ich głos tylko przy pewnym rozpoznaniu; każda inna kwestia — lektor.
- Nie-cel: odkrywanie innych postaci i zbieranie próbek w trakcie gry (stary tryb klonowania zostaje osobno).
- Anty-cel: cisza zamiast lektora (stary tryb ścisły „cloneOnly”) — tu nigdy.
- Anty-cel: głos bohatera przy niepewnym dopasowaniu (zła postać mówi głosem Jasona).

## Podejścia (rozważone)
1. Gotowe próbki (zwiastun) + rozpoznanie po imieniu w napisie i po barwie głosu — **wybrane**.
2. Stary SpeakerBank: odkrywanie postaci i próbek z gry — odrzucone: niepewne próbki, cisza, Parakeet 1,5 GB.
3. Spolszczenie barwy (konwersja głosu) — odrzucone przez użytkownika („tak jak jest”).

## Jak (decyzje)
- Próbki: `GameReader/heroes/<jason|lucia>/sample.wav` (24 kHz) + `sample.txt`; poza listą głosów lektora.
  Do procesu VoiceStudio idą plikiem w pełnej jakości (nie przez zapis 16 kHz jak zwykłe klony).
- Rozpoznanie: (a) imię z dwukropkiem przed kwestią („JASON: …”, „Lucia: …”) — pewne; (b) wektor mówcy
  (wespeaker, jak SpeakerBank) wypowiedzi z gry najbliższej w czasie pojawieniu się napisu vs wektor próbki;
  bohater tylko przy podobieństwie ≥ próg (start 0,45: zwiastun Jason↔Jason 0,53–0,85, strażnik↔Jason 0,16–0,22).
  Każda decyzja w logu (podobieństwo) — do strojenia na YouTube/grze.
- Przy polskich napisach bez transkrypcji Parakeetem (sama barwa).
- Głos lektora nie trafia do porównań: nasłuch bierze dźwięk samego procesu gry (Chrome / PS Remote Play), a lektor gra z procesu LiveDub.

## Zostawione budującemu
Dokładne miejsce zapamiętania imienia przy wycinaniu, nazwy stałych, wygląd przełącznika obok „Klonuj głosy postaci”.
