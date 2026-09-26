# LiveDub — polski lektor na żywo

Pobierz plik z sekcji **Assets** najnowszego wydania:

- **Mac** (Apple Silicon M1 lub nowszy, macOS 14.2+): `LiveDub-mac-arm64.zip`
- **Windows** (10/11, 64-bit): `LiveDub-Setup-win-x64.exe`

LiveDub nie jest podpisany płatnym certyfikatem Apple ani Microsoftu, więc przy pierwszym
uruchomieniu system ostrzeże, że nie zna wydawcy. To jednorazowe.

## Mac

1. Rozpakuj `LiveDub-mac-arm64.zip` (dwuklik) i przeciągnij **LiveDub** do folderu **Aplikacje**.
2. Otwórz LiveDub. macOS pokaże, że nie może go zweryfikować — kliknij **Gotowe**
   (nie „Przenieś do kosza”).
3. Wejdź w **Ustawienia systemowe → Prywatność i ochrona**, przewiń na dół i przy
   „LiveDub został zablokowany…” kliknij **Otwórz mimo to**, potem podaj hasło.

   Jeśli tego przycisku nie ma, wklej w Terminalu i otwórz LiveDub jeszcze raz:

   ```
   xattr -dr com.apple.quarantine /Applications/LiveDub.app
   ```

4. Zezwól na **Nagrywanie ekranu i dźwięku** (i ewentualnie mikrofon), gdy macOS zapyta,
   potem uruchom LiveDub ponownie.

## Windows

1. Uruchom `LiveDub-Setup-win-x64.exe`.
2. Jeśli pojawi się „System Windows ochronił ten komputer”, kliknij **Więcej informacji → Uruchom mimo to**.

## Pierwsze uruchomienie

LiveDub sam pobiera swój silnik i modele (rozpoznawanie mowy, tłumacz, lektor) — kilka GB,
kilka–kilkanaście minut, potrzebny internet. Kolejne starty są szybkie, a nowe wersje
instalują się same.
