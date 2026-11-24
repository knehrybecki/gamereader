"""21.11: OCR Apple (Vision) pod presją pamięci zwracał pustkę i ładował model od nowa po 14–37 s — lektor „zaczął czytać
i przestał”. Na życzenie użytkownika LiveDub czyta napisy PaddleOCR (PP-OCRv5, jak GameReader). Model łaciński myli
polskie litery z czeskimi („tež”, „že”) i „ł” z „t” („odpalita”) — poprawia je słownik słów z napisów filmowych
(pl_lex.sqlite: częstość słów i sąsiedzi rozstrzygający formę, tools/build_pl_lexicon.py)."""
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
                         "X Dobra. Załóżmy, że chcę jedną z tych bryk.")  # „jedna” → „jedną” wybiera sąsiad „chcę”

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

    def test_letters_misread_by_ocr(self):
        # 24.11 z logu (v1.3.108): słownik bez częstości nie poprawiał „cate”/„byto” (są w napisach jako literówki),
        # a „ł” bywa odczytane jako „f”, „ź” jako „ś”, „ń” jako „r”, a „ź”/„ż” potrafi zniknąć
        for read, want in (
            ("że gdy cię widzę, to cate ciało mnie łaskocze,", "że gdy cię widzę, to całe ciało mnie łaskocze,"),
            ("Poza tym już mu o tobie opowiedziałem, podczas gdy cię nie byto.",
             "Poza tym już mu o tobie opowiedziałem, podczas gdy cię nie było."),
            ("Byt niskim chłopaczkiem.", "Był niskim chłopaczkiem."),
            ("Cafy dzień, byfo super, zapomniafem.", "Cały dzień, było super, zapomniałem."),
            ("Użyj wyobraśni. Sprawdś to.", "Użyj wyobraźni. Sprawdź to."),
            ("Znaleśliśmy go, weśmie to.", "Znaleźliśmy go, weźmie to."),
            ("Przez ciebie się spónię!", "Przez ciebie się spóźnię!"),
            ("Wiem, cięko jest.", "Wiem, ciężko jest."),
            ("Zadanie zakorczone.", "Zadanie zakończone."),
        ):
            self.assertEqual(latin_ocr_polish(read), want)

    def test_form_chosen_from_neighbours(self):
        # „woda”/„wodą”, „ze”/„że” — obie formy poprawne, wybiera sąsiednie słowo
        for read, want in (
            ("Dobra, wiem, ze mnie tu nie ściągasz.", "Dobra, wiem, że mnie tu nie ściągasz."),
            ("Ot, problem z dzisiejsza młodzieżą!", "Ot, problem z dzisiejszą młodzieżą!"),
            ("Tu masz główna scenę.", "Tu masz główną scenę."),
            ("Spotkałem się z twoja siostrą.", "Spotkałem się z twoją siostrą."),
            ("Wpuść ja, Dook.", "Wpuść ją, Dook."),
        ):
            self.assertEqual(latin_ocr_polish(read), want)
        for text in ("Idę ze mną.", "Woda jest zimna.", "Główna scena.", "Jak na laskę, której kariera to wpadka.",
                     "Ja nie wiem.", "Dobra robota."):
            self.assertEqual(latin_ocr_polish(text), text)

    def test_names_and_real_words_with_lookalike_letters_stay(self):
        for text in ("Widziałeś Cate? Cate Blanchett tu była.", "On śle list.", "To mój dar.", "Ten bat jest stary."):
            self.assertEqual(latin_ocr_polish(text), text)

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
