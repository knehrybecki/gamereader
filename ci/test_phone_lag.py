"""27.11 (GTA VI, użytkownik: „lektor sobie coś gada, a nie ma napisów” i „dalej są opóźnienia”).

1. Telefon w rogu ekranu wchodzi w pasek napisów. Lektor czytał ekran połączenia („Dzwoni…”, „Lorena Połączenie
   za”, „Mamy już ciebie dosyć! Randy Dzwoni…”) i ucięte SMS-y („Taa, zadowolon”, „Numer zastrz Odbierzcie to V
   Savannah na wysp”) — 37 razy w logach z 25–27.11. Użytkownik: SMS-y pomijać.
2. Zapas (synteza od pierwszego odczytu OCR) robił się też dla okruchów z kadru („f (n O”, 2 s syntezy), a jedyny
   proces VoiceStudio był wtedy zajęty — prawdziwa kwestia ruszyła 3,8 s po napisie.
3. Sklejone kwestie („Pewnie.” + „Świetnie. Tu jest lepsze światło, chodź…”) miały gotowe nagrania z zapasu, a lektor
   syntezował całość od nowa (2,6 s) i ruszył 4,9 s po napisie."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import Engine, PhoneScreen, _join_parts


class StopLoop(BaseException):
    pass


class PhoneCallScreenTest(unittest.TestCase):
    def test_call_status_alone_is_not_a_line(self):
        for read in ("Dzwoni…", "Połączenie za", "Połączenie z", "Połączenie", "Lorena Połączenie za",
                     "Jason Połączenie Za", "Yasmani Dzwoni…", "Lucia Wybieranie…", "Lorena :", "Lorena"):
            self.assertEqual(PhoneScreen().strip(read, now=0.0), "", read)

    def test_dialogue_before_the_call_status_stays(self):
        for read, line in (
            ("Szmata tylko wkurwia ostre suki, nie? Dook Połączenie za", "Szmata tylko wkurwia ostre suki, nie?"),
            ("Mamy już ciebie dosyć! Randy Dzwoni…", "Mamy już ciebie dosyć!"),
            ("Uf, cholera… Jason Połączenie z", "Uf, cholera…"),
            ("Udaj się na teren kompleksu. Połączenie", "Udaj się na teren kompleksu."),
            ("Trzeba było. Ja i Jason jesteśmy wam bardzo wdzięczni. Lorena :",
             "Trzeba było. Ja i Jason jesteśmy wam bardzo wdzięczni."),
            # rozmowa grupowa
            ("Nadal masz czas, napaleńcu. Właśnie rozmawialiśmy o interesach z Jasonem i panem Ike. Yasmani Konferencja:",
             "Nadal masz czas, napaleńcu. Właśnie rozmawialiśmy o interesach z Jasonem i panem Ike."),
            ("Wpadnę. Yasmani Konferencja:", "Wpadnę."),
            # nagłówek rozmowy w jednym wierszu z napisem: „Jason … :” — to kwestia, nie SMS
            ("Jason Serio? :", "Serio?"),
            ("Podeślę ulotkę. Dre'Quan :", "Podeślę ulotkę."),
            ("Dre'Quan Połączenie Z", ""),
            # okruchy OCR przy nagłówku rozmowy
            ("Laska, co ty? To mega. N Andy DK :", "Laska, co ty? To mega."),
            ("Pani od antyków? D poros Evelyn endli :", "Pani od antyków?"),
        ):
            self.assertEqual(PhoneScreen().strip(read, now=0.0), line, read)

    def test_ordinary_dialogue_is_untouched(self):
        for line in (
            "Suko, czemu do mnie podskakujesz? Dzwoniła Evelyn. Jest wściekła na Halifaxa.",
            "Kto dzwoni?",
            "Połączenie z bazą zerwane.",
            "Lucita. Nie trzeba było, ale dziękuję.",
            "Lorena, jeszcze raz dzięki.",
            "Jason, chodź tutaj!",
            "Trzeba było. Ja i Jason jesteśmy wam bardzo wdzięczni.",
        ):
            self.assertEqual(PhoneScreen().strip(line, now=0.0), line)


class PhoneMessagesTest(unittest.TestCase):
    def test_messages_under_a_contact_name_are_skipped(self):
        for read in (
            "Lorena Taa, zadowolon Pełnia szczęści",
            "M Lorena Jeszcze raz dzięki Byliśmy z dzieciak; farmie. Tommy'em",
            "Lorena Pierdol się, liberalna pato! Jeszcze raz dzięki",
            "DK N Andy To mój nowy Mieszkam kot pokazowego",
            "Kyle Otrzymano 250 $",
            "Yasmani Konferencja:",
        ):
            self.assertEqual(PhoneScreen().strip(read, now=0.0), "", read)

    def test_contact_learned_from_the_call_screen(self):
        phone = PhoneScreen(contacts=())
        self.assertEqual(phone.strip("Ay Cześć, tití. Marisol :", now=0.0), "Ay Cześć, tití.")
        self.assertEqual(phone.strip("Marisol Zrobiłam wczoraj tę dwójkę.", now=10.0), "")

    def test_withheld_number_message_and_its_later_fragment(self):
        phone = PhoneScreen()
        self.assertEqual(phone.strip("Numer zastrz Odbierzcie to V Savannah na wysp Waxman. Kod to", now=0.0), "")
        self.assertEqual(phone.strip("Odbierzcie to V Savannah na wysp", now=4.0), "")
        # po minucie z okładem ten sam tekst to już nie SMS sprzed chwili
        self.assertEqual(phone.strip("Odbierzcie to V Savannah na wysp", now=200.0), "Odbierzcie to V Savannah na wysp")

    def test_message_fragment_without_the_header(self):
        phone = PhoneScreen()
        self.assertEqual(phone.strip("Lorena Taa, zadowolon Pełnia szczęści", now=0.0), "")
        self.assertEqual(phone.strip("Taa, zadowolon", now=1.0), "")

    def test_subtitle_pipeline_drops_the_phone(self):
        engine = Engine.__new__(Engine)
        engine._source_kind = lambda: "ps"
        engine._log_junk = Mock()
        engine.emit = Mock()
        engine._lead_junk = SimpleNamespace(observe=lambda *_: None, strip=lambda text: text)
        engine._worse_reread = Mock(side_effect=AssertionError("telefon miał odpaść wcześniej"))
        for read in ("Lorena Połączenie za", "Lorena Taa, zadowolon Pełnia szczęści", "Dzwoni…"):
            engine._on_subtitle(read)
        engine._worse_reread.assert_not_called()


class JunkReadSpeculationTest(unittest.TestCase):
    def engine(self):
        engine = Engine.__new__(Engine)
        engine._spec_redo, engine._spec_rank, engine._spec_key = 0, -1, ""
        engine._like_spoken = lambda *_: False
        engine._is_current = lambda *_: False
        engine._seen_at = {}
        engine.brain = None
        engine._speculate = Mock()
        return engine

    def test_scene_crumb_gets_no_synthesis(self):
        engine = self.engine()
        engine._speculate_candidate("f (n O", fresh=True)
        engine._speculate.assert_not_called()

    def test_short_real_lines_still_get_one(self):
        for line in ("No i co?", "Mordo! Z drogi!", "Weź telefon od Andy."):
            engine = self.engine()
            engine._speculate_candidate(line, fresh=True)
            engine._speculate.assert_called_once()


def loop_engine(parts, ready):
    engine = Engine.__new__(Engine)
    engine.has_pending = threading.Event()
    engine.has_pending.set()
    calls = iter([(_join_parts(parts), False, parts[-1], list(parts))])

    def take():
        try:
            return next(calls)
        except StopIteration:
            raise StopLoop

    engine._take_pending = take
    engine._fresh_parts = lambda p: p
    engine.speaking_text = ""
    engine._speaking_parts = []
    engine._spec_done = threading.Condition()
    engine._spec_busy = None
    engine._ready_lock = threading.Lock()
    engine._ready = dict(ready)
    engine._timing = lambda *_: None
    engine.mode = "ocr"
    engine._pl_subs_at = None
    engine._grown_version = lambda *_: False
    engine._queue = []
    engine.pending_lock = threading.Lock()
    engine.no_barge_in = False
    engine._tts_interrupt = threading.Event()
    engine._seen_at = {}
    engine._film_entry = lambda *_: 0.0
    engine._lag = 0.0
    engine._remember_sentences = lambda *_: None
    engine.emit = Mock()
    engine._duck_on = engine._duck_release = lambda: None
    engine._speech_gen = 0
    engine._prefetch_pending = lambda *_: None
    engine._wait_segment = lambda *_: False
    engine._mark_spoken = lambda *_: None
    engine._queue_waiting = lambda *_: False
    engine._brain_junk = lambda *_, **__: False
    engine.lektor_volume = 80
    engine.played = []
    engine.lektor = SimpleNamespace(
        cps1=14.0, tts_scale=1.0, busy_until=0.0, play=engine.played.append,
        prepare=Mock(side_effect=lambda text, **_: f"nowe:{text}"),
    )
    engine._plan_line = Mock(side_effect=lambda src, translate, parts=None: (src, [(src, "calm")], 0.0, 1.45))
    return engine


class MergedLinesUseReadyClipsTest(unittest.TestCase):
    PARTS = ["Pewnie.", "Świetnie. Tu jest lepsze światło, chodź…"]

    def test_both_ready_play_without_new_synthesis(self):
        ready = {p: (p, [(p, "calm")], 0.0, 1.45, f"zapas:{p}") for p in self.PARTS}
        engine = loop_engine(self.PARTS, ready)
        with self.assertRaises(StopLoop):
            engine._tts_loop()
        self.assertEqual(engine.played, [f"zapas:{p}" for p in self.PARTS])
        engine.lektor.prepare.assert_not_called()
        spoken = engine.emit.call_args_list[0].args[0]["text"]
        self.assertEqual(spoken, " ".join(self.PARTS))

    def test_missing_later_part_is_made_while_the_first_plays(self):
        first = self.PARTS[0]
        engine = loop_engine(self.PARTS, {first: (first, [(first, "calm")], 0.0, 1.45, f"zapas:{first}")})
        with self.assertRaises(StopLoop):
            engine._tts_loop()
        self.assertEqual(engine.played, [f"zapas:{first}", f"nowe:{self.PARTS[1]}"])
        engine._plan_line.assert_called_once_with(self.PARTS[1], False, [self.PARTS[1]])

    def test_without_ready_first_part_the_whole_is_synthesized_as_before(self):
        engine = loop_engine(self.PARTS, {})
        with self.assertRaises(StopLoop):
            engine._tts_loop()
        joined = _join_parts(self.PARTS)
        self.assertEqual(engine.played, [f"nowe:{joined}"])
        engine._plan_line.assert_called_once_with(joined, False, self.PARTS)


if __name__ == "__main__":
    unittest.main()
