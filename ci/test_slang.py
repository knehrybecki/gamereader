"""Slang → zwykły angielski przed Argos. Tylko przepisania, które na prawdziwym Argos dały lepszy polski wynik (09.10)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine, normalize_slang


class SlangTest(unittest.TestCase):
    CASES = {
        "I'm gon roll me a fat one.": "I'm going to make myself a big joint.",
        "'im gon roll me a fat one.": "I'm going to make myself a big joint.",
        "Iim gon roll me a fat one.": "I'm going to make myself a big joint.",
        "He's gonna roll him a fat one.": "He's gonna make him a big joint.",
        "lim gon rall me a fat ane.": "I'm going to make myself a big joint.",
        "U'm gon roll me a fat one.": "I'm going to make myself a big joint.",
        "'m gon roll me a fat one.": "I'm going to make myself a big joint.",
        "lim. gon rell me a fat ane.": "I'm going to make myself a big joint.",
        "Hey, one of y'all down?": "Hey, is anyone of you in?",
        "Y'all ready?": "You guys ready?",
        "Nice whip, man.": "Nice car, man.",
        "Welcome to my crib.": "Welcome to my house.",
        "Get that paper.": "Get that money.",
        "He's strapped.": "He's armed.",
        "Grab a piece.": "Grab a gun.",
        "We gotta boost a car.": "We gotta steal a car.",
        "Somebody's gonna get clapped.": "Somebody's gonna get shot.",
        "This is sus.": "This is suspicious.",
        "Don't be salty.": "Don't be bitter.",
        "I'm tryna get paid.": "I'm trying to get paid.",
        "He's a real OG.": "He's a real veteran.",
        "What's good, homie?": "How are you, homie?",
        "Nearly went pro.": "Nearly became a professional.",
        "You're trippin'.": "You're crazy.",
        "This place is lit.": "This place is great.",
        "No cap, he's a legend.": "Honestly, he's a legend.",
        "Stay frosty out there.": "Stay alert out there.",
        "We got heat on us.": "The police are after us.",
        "Imma head out.": "I'm going to head out.",
    }

    def test_known_slang_is_rewritten(self):
        for src, want in self.CASES.items():
            self.assertEqual(normalize_slang(src), want, src)

    def test_ordinary_text_is_untouched(self):
        for text in (
            "Hey, Billy! Yo, what's up?", "He's a snitch, man. Everybody knows.", "Chill, I got this.",
            "Dude, we're so screwed.", "Let's roll. Get in.", "Bro, that's sketchy as hell, I'm out.",
            "What the fuck you lookin' at?", "Nice to meet you guys.", "Tell me about it.",
        ):
            self.assertEqual(normalize_slang(text), text, text)

    def test_ambiguous_words_are_left_alone_without_slang_context(self):
        for text in (
            "She used a whip on the horse.", "The baby's crib is empty.", "Give me a piece of cake.",
            "Pass me the paper.", "He was strapped for cash.", "A rolling stone.", "Wait for the heat to pass.",
            "The lamp is lit.", "I don't have a cap.", "Don't use the clap.",
        ):
            self.assertEqual(normalize_slang(text), text, text)

    def test_capitalisation_and_punctuation_survive(self):
        self.assertEqual(normalize_slang("SUS."), "Suspicious.")
        self.assertEqual(normalize_slang("Y’all ready?"), "You guys ready?")  # typograficzny apostrof z OCR
        self.assertEqual(normalize_slang(""), "")


class SlangInPlanLineTest(unittest.TestCase):
    def test_translator_receives_the_rewritten_text(self):
        seen = []
        engine = Engine.__new__(Engine)
        engine.translator = Mock(translate=lambda t: seen.append(t) or "Zrobię sobie duży joint.")
        engine.lektor = Mock(plan=lambda t: [(t, "calm")], overload=lambda *_a: 0.0, line_boost=lambda *_a: 1.0)
        engine.prosody = Mock(recent=Mock(return_value=0.0))
        engine._heard_arousal = {}
        engine.catch_up = False
        engine._screen_budget = Mock(return_value=6.0)
        engine._timing = Mock()
        engine.brain = None
        engine._plan_line("I'm gon roll me a fat one.", True)
        self.assertEqual(seen, ["I'm going to make myself a big joint."])


if __name__ == "__main__":
    unittest.main()
