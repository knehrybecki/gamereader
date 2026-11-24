"""21.11: OCR Apple (Vision) pod presją pamięci zwracał pustkę i ładował model od nowa po 14–37 s — lektor „zaczął czytać
i przestał”. Na życzenie użytkownika LiveDub czyta napisy PaddleOCR (PP-OCRv5, jak GameReader). Model łaciński myli
polskie litery z czeskimi („tež”, „že”) i „ł” z „t” („odpalita”) — poprawia je słownik słów z napisów."""
import importlib.util
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge
from gamereader_engine import latin_ocr_polish


class LatinToPolishTest(unittest.TestCase):
    def test_czech_letters_become_polish(self):
        self.assertEqual(latin_ocr_polish("Každa taka tež zostaw w spokoju."), "Każda taka też zostaw w spokoju.")
        self.assertEqual(latin_ocr_polish("že chcę"), "że chcę")

    def test_t_misread_for_l_with_stroke(self):
        self.assertEqual(latin_ocr_polish("Nie wiem. Wybiłabym szybę i odpalita na krótko?"),
                         "Nie wiem. Wybiłabym szybę i odpaliła na krótko?")
        self.assertEqual(latin_ocr_polish("X Dobra. Zatóžmy, že chcę jedna z tych bryk."),
                         "X Dobra. Załóżmy, że chcę jedna z tych bryk.")

    def test_lost_or_wrong_ogonki_from_dictionary(self):
        # 24.11 z logu: model łaciński gubi ogonki albo myli „ź” z „ż” — słownik zna jedyną poprawną formę
        self.assertEqual(latin_ocr_polish("Oho, gliny się angażuja."), "Oho, gliny się angażują.")
        self.assertEqual(latin_ocr_polish("Oho, gliny się angazuja."), "Oho, gliny się angażują.")
        self.assertEqual(latin_ocr_polish("Nie przyjażnimy się."), "Nie przyjaźnimy się.")
        self.assertEqual(latin_ocr_polish("nowy strój przy stoisku z gadzetami."),
                         "nowy strój przy stoisku z gadżetami.")
        self.assertEqual(latin_ocr_polish("DODAJ DO BAGAZNIKA"), "DODAJ DO BAGAŻNIKA")
        self.assertEqual(latin_ocr_polish("Weż to i chodż."), "Weź to i chodź.")
        # odczytany ogonek zostaje: „zęby” (w słowniku pod „zeby” jest tylko „żeby”) to inne słowo
        self.assertEqual(latin_ocr_polish("Umyj zęby."), "Umyj zęby.")

    def test_english_text_keeps_letters(self):
        # „zone” w słowniku to „żonę” — angielskiego tekstu (idzie do tłumaczenia) nie ruszamy
        for text in ("Stay in the zone, man.", "We made it to the city."):
            self.assertEqual(latin_ocr_polish(text), text)

    def test_real_words_and_names_stay(self):
        for text in ("Mam kota.", "Tata tu był.", "Ci Watkins", "Jason, to twoja fura?", "Trzeba to zrobić."):
            self.assertEqual(latin_ocr_polish(text), text)


@unittest.skipUnless(importlib.util.find_spec("rapidocr"), "brak rapidocr w tym środowisku")
class PaddleOcrReadTest(unittest.TestCase):
    def test_reads_subtitle_band_with_polish_letters(self):
        img = Image.new("RGB", (1194, 145), (60, 70, 80))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 34)
        text = "Jezu Chryste, jeszcze kogoś zabijesz!"
        draw.text((260, 80), text, font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
        ocr = ge.PaddleOcr()
        frame = __import__("numpy").asarray(img)[:, :, ::-1].copy()
        self.assertEqual(ocr.read(frame), text)


if __name__ == "__main__":
    unittest.main()
