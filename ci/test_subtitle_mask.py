"""Filtr wyglądu napisów: kolorowe litery i jasne tablice nie trafiają do OCR."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import AppleVisionOcr, WindowsOcr


def fixture(color=(245, 245, 245), background=(30, 60, 90), outline=True):
    image = Image.new('RGB', (600, 120), background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    draw.text((50, 40), 'To tutaj. Chodz do domu!', font=font, fill=color,
              stroke_width=2 if outline else 0, stroke_fill=(0, 0, 0))
    return np.asarray(image)[:, :, ::-1].copy()


class SubtitleMaskTest(unittest.TestCase):
    def test_white_letters_on_dark_background_survive(self):
        self.assertIsNotNone(AppleVisionOcr.subtitle_mask(fixture(outline=False, background=(20, 20, 20))))

    def test_outline_preserves_white_letters_on_bright_scene(self):
        frame = fixture(background=(150, 185, 210))
        self.assertIsNotNone(AppleVisionOcr.subtitle_mask(frame))

    def test_colored_letters_do_not_reach_ocr_or_unfiltered_fallback(self):
        ocr = AppleVisionOcr()
        ocr._run_items = Mock(return_value=[])
        for color in ((255, 220, 0), (255, 70, 60), (40, 255, 70), (100, 180, 255)):
            self.assertEqual(ocr.read(fixture(color=color)), '')
        ocr._run_items.assert_not_called()

    def test_bright_letters_without_dark_outline_are_rejected(self):
        ocr = AppleVisionOcr()
        ocr._run_items = Mock(return_value=[])
        self.assertEqual(ocr.read(fixture(background=(160, 170, 190), outline=False)), '')
        ocr._run_items.assert_not_called()

    def test_dense_white_plate_is_not_treated_as_white_glyphs(self):
        frame = np.full((120, 600, 3), 20, dtype=np.uint8)
        frame[40:65, 50:130] = 245
        frame[40:65, 55:130:8] = 0
        ocr = AppleVisionOcr()
        ocr._run_items = Mock(return_value=[(0, 50, 'San Andreas', (50, 40, 80, 25), .9)])
        self.assertEqual(ocr.read(frame), '')

    def test_windows_uses_same_strict_filter(self):
        self.assertIs(WindowsOcr.read, AppleVisionOcr.read)
        self.assertIs(WindowsOcr.subtitle_mask, AppleVisionOcr.subtitle_mask)

    def test_outlined_game_subtitle_is_read_from_the_color_frame(self):
        ocr = AppleVisionOcr()
        seen = []

        def run(image, *_args, **_kwargs):
            seen.append(np.asarray(image))
            return [(0, 40, "Wpadłem na drinka, którego proponowałeś.", (50, 30, 480, 50), 0.9)]

        ocr._run_items = run
        frame = fixture(background=(180, 160, 120), outline=True)
        self.assertIn("Wpadłem", ocr.read(frame))
        colors = seen[0]
        self.assertGreater(len(np.unique(colors[:, :, 0])), 4)


if __name__ == '__main__':
    unittest.main()
