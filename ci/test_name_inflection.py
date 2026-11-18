"""Odmienione angielskie imiona w lektorze VoiceStudio (18.11: „Brianem” czytane jak napisane). Samo imię zostaje
w oryginale (OmniVoice mówi je po angielsku), odmiana idzie z polskiego zapisu wymowy — automatycznie, bez
dopisywania każdej formy."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import voicestudio_names


class NameInflectionTest(unittest.TestCase):
    def test_inflected_listed_names_use_polish_spelling(self):
        self.assertEqual(voicestudio_names("Pogadaj z Brianem."), "Pogadaj z Brajanem.")
        self.assertEqual(voicestudio_names("Daj to Michaelowi, a potem Brianowi."), "Daj to Majkelowi, a potem Brajanowi.")
        self.assertEqual(voicestudio_names("Nie ma Jasona."), "Nie ma Dżejsona.")

    def test_bare_names_stay_english(self):
        self.assertEqual(voicestudio_names("Brian czeka w Vice City."), "Brian czeka w Vice City.")
        self.assertEqual(voicestudio_names("Jason, wsiadaj."), "Jason, wsiadaj.")

    def test_lucia_keeps_spanish_reading(self):
        self.assertEqual(voicestudio_names("Lucię już złapali."), "Lus'iję już złapali.")

    def test_unlisted_english_looking_names_are_respelled_when_inflected(self):
        self.assertEqual(voicestudio_names("Spotkałem się z Heathem."), "Spotkałem się z Hitem.")
        self.assertEqual(voicestudio_names("Zadzwoń do Shawna."), "Zadzwoń do Szona.")

    def test_polish_words_and_names_untouched(self):
        for text in ("Teatrem byłem zachwycony.", "Idę z Kowalskim do Wojtka.", "Spotkamy się w Warszawie."):
            self.assertEqual(voicestudio_names(text), text)


if __name__ == "__main__":
    unittest.main()
