"""Regresje OCR z sesji LiveDub; bez modeli, dźwięku i przechwytywania ekranu."""
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gamereader_engine import (
    Engine, RecurringFragments, strip_lead_junk, screen_junk_level, same_utterance, ocr_reading_rank,
    trim_ocr_edges, condense_polish, lektor_speed_split, LEKTOR_MAX_RATE, LEKTOR_MAX_STRETCH,
    strip_known_prefix, ocr_reading_unsettled, repair_polish_ocr, RecurringLead, extends_utterance,
    strip_hud_prompts,
)


class SubtitleFiltersTest(unittest.TestCase):
    def test_transient_plate_fragment_needs_fresh_confirmation(self):
        engine = Engine.__new__(Engine)
        engine.mode = "ocr"
        engine._pl_subs_at = None
        engine._source_kind = lambda: "ps"
        engine._recurring = RecurringFragments()
        engine.speaking_full = engine.last_full = engine._ocr_candidate = ""
        engine._ocr_candidate_n = 0
        engine._seen_at = {}
        engine._sub_prev = None
        engine.confirm_frames = 1
        engine._grown_from_recent = lambda *_: None
        engine._recently_spoken = lambda *_: False
        engine._speculate = Mock()
        engine._offer_line = Mock()
        engine.emit = Mock()
        with patch("gamereader_engine.time.monotonic", return_value=10.0):
            engine._on_subtitle("Andersi,")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=12.0):
            engine._on_subtitle("Andersi,")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=12.15):
            engine._on_subtitle("Andersi,")
        engine._offer_line.assert_called_once()
        engine._offer_line.reset_mock()
        engine._sub_prev = None
        with patch("gamereader_engine.time.monotonic", return_value=13.0):
            engine._on_subtitle("To tutaj.")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=13.45):
            engine._on_subtitle("To tutaj.")
        engine._offer_line.assert_called_once_with("To tutaj.", False)

    def test_short_dialogue_ocr_variants_are_not_repeated(self):
        for variant in ("To tutai.", "To tutal."):
            self.assertTrue(same_utterance("To tutaj.", variant))
            self.assertTrue(same_utterance(variant, "To tutaj."))
        for different in ("To jutro.", "To tam.", "Nie tutaj."):
            self.assertFalse(same_utterance("To tutaj.", different))
        self.assertFalse(same_utterance("On stoi.", "On stój."))

    def test_long_ocr_prefixes_do_not_turn_dialogue_into_watermark(self):
        dialogue = "Może kiedyś przy browarku wyłuszczę ci, jak działa ten świat."
        fragments = RecurringFragments()
        for prefix in ("OrtleLLI PrEME ", "PRZEGLADANIE ", ""):
            text = prefix + dialogue
            self.assertEqual(fragments.strip(text), text)

    def test_logged_hud_prefixes_preserve_dialogue(self):
        dialogue = "Więc raczej odstawiasz przekręt z zadłużeniem."
        fragments = RecurringFragments()
        for prefix in ("SIELE ", "tANG ", "SĘENCE ", "PARNE ", "PLENE ", ""):
            self.assertEqual(fragments.strip(strip_lead_junk(prefix + dialogue)), dialogue)

    def test_watermark_on_different_dialogues_is_still_removed(self):
        watermark = "Subskrybuj nasz kanał już teraz"
        fragments = RecurringFragments()
        for dialogue in ("Jedź szybko do garażu, bo nas dogonią.",
                         "Muszę kupić coś dobrego na dzisiejszą kolację."):
            text = dialogue + " " + watermark
            self.assertEqual(fragments.strip(text), text)
        dialogue = "Przestań strzelać, tam są nasi ludzie!"
        self.assertEqual(fragments.strip(dialogue + " " + watermark), dialogue)
        self.assertEqual(fragments.strip(watermark), "")

    def test_menu_from_live_log_is_rejected(self):
        self.assertEqual(screen_junk_level("PRZEGLĄDAJ L R UKRYJ MENU"), "junk")

    def _engine(self):
        engine = Engine.__new__(Engine)
        engine.mode = "ocr"
        engine._pl_subs_at = None
        engine._source_kind = lambda: "ps"
        engine._recurring = RecurringFragments()
        engine.speaking_full = engine.last_full = engine._ocr_candidate = ""
        engine._ocr_candidate_n = 0
        engine._ocr_samples = []
        engine._seen_at = {}
        engine._sub_prev = None
        engine.confirm_frames = 1
        engine._grown_from_recent = lambda *_: None
        engine._recently_spoken = lambda *_: False
        engine._speculate = Mock()
        engine._offer_line = Mock()
        engine.emit = Mock()
        engine._junk_logged = ""
        engine._timing = Mock()
        return engine

    def test_clean_line_is_read_immediately(self):
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=10.0):
            engine._on_subtitle("To się nazywa kapitalizm.")
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=10.45):
            engine._on_subtitle("To się nazywa kapitalizm.")
        engine._offer_line.assert_called_once_with("To się nazywa kapitalizm.", False)

    def test_blurred_first_frame_waits_for_a_cleaner_reading(self):
        engine = self._engine()
        frames = [
            (10.00, "spontanfcznego ożywienia zwłok, które zaczęły straszyåwezyalich wøköt, takø"),
            (10.18, "spontanfcznego ozywienia zwłok, które zaczęły straszydwezjafichwkól, taką"),
            (10.51, "spontanicznego ożywienia zwłok, które zaczęły straszyć wezykich wekö, tak?"),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        engine._offer_line.assert_not_called()
        better = "spontanicznego ożywienia zwłok, które zaczęły straszyć wezystich woket, tak?"
        self.assertGreater(ocr_reading_rank(better), ocr_reading_rank(frames[0][1]))
        with patch("gamereader_engine.time.monotonic", return_value=10.70):
            engine._on_subtitle(better)
        spoken = engine._offer_line.call_args[0][0]
        self.assertIn("spontanicznego", spoken)
        self.assertNotIn("spontanf", spoken)

    def test_first_misspelled_frame_yields_to_the_next(self):
        engine = self._engine()
        frames = [
            (40.00, "Bobra, Idziemy."),
            (40.16, "Dobra, idziemy."),
            (40.70, "Dobra, idziemy."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        spoken = [call.args[0] for call in engine._offer_line.call_args_list]
        self.assertTrue(spoken)
        self.assertTrue(all(text == "Dobra, idziemy." for text in spoken))

    def test_garbled_car_line_is_replaced_by_the_clear_one(self):
        engine = self._engine()
        frames = [
            (50.00, "Weżmy aaro Amtindy."),
            (50.15, "Weźmy auto Amandy."),
            (50.70, "Weźmy auto Amandy."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        spoken = [call.args[0] for call in engine._offer_line.call_args_list]
        self.assertTrue(spoken)
        self.assertTrue(all("auto Amandy" in text and "Amtindy" not in text for text in spoken))

    def test_garbled_reread_without_polish_words_is_skipped(self):
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=30.0):
            engine._on_subtitle("Thy co wedy atak eles.")
        engine._offer_line.assert_not_called()

    def test_leading_ocr_mark_is_removed(self):
        self.assertEqual(trim_ocr_edges("¡To dlatego siedzisz w Vinewood?"), "To dlatego siedzisz w Vinewood?")

    def test_no_to_is_not_condensed_away(self):
        text = "No to morałem dzisiejszej lekcji niech będzie pokora."
        self.assertTrue(condense_polish(text, level=1).lower().startswith("no to"))

    def test_flickering_variants_are_not_joined_into_one_line(self):
        engine = self._engine()
        frames = [
            (20.00, "z mógł kantować ludzli na tymzarablać. tAredy bedkles"),
            (20.16, "Wtedy będklesz mógł kantować ludili na tymzarablać."),
            (20.70, "Wtedy będziesz mógł kantować ludzi i na tym zarabiać."),
            (20.90, "Wtedy będziesz mógł kantować ludzi i na tym zarabiać."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        engine._offer_line.assert_called_once()
        spoken = engine._offer_line.call_args[0][0]
        self.assertIn("będziesz", spoken)
        self.assertNotIn("bedkles", spoken)

    def test_game_line_is_synthesized_while_ocr_settles(self):
        # sesja 15:15: bez syntezy na zapas lektor startował 1,3 s po napisie zamiast ~0,4 s
        engine = self._engine()
        frames = [
            (12.14, "Jeśli mam to zrobić, to musisz podjechać olizej."),
            (12.30, "Jeśli mam to zrobić, to musisz podjechać bliżej."),
            (12.60, "Jeśli mam to zrobić, to musisz podjechać bliżej."),
        ]
        for stamp, text in frames:
            with patch("gamereader_engine.time.monotonic", return_value=stamp):
                engine._on_subtitle(text)
        spoken = engine._offer_line.call_args[0][0]
        speculated = [call.args[0] for call in engine._speculate.call_args_list]
        self.assertEqual(speculated[-1], spoken)
        self.assertEqual(len(speculated), len(set(speculated)))

    def test_clean_line_read_once_before_hud_frames_is_not_lost(self):
        # sesja 15:17: „Zabieraj stąd swoje dupsko.” raz, potem „Mitun”, „lin” — lektor milczał
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=38.00):
            engine._on_subtitle("Zabieraj stąd swoje dupsko.")
            engine._flush_candidate()
        engine._offer_line.assert_not_called()
        with patch("gamereader_engine.time.monotonic", return_value=38.45):
            engine._on_subtitle("Mitun")
            engine._flush_candidate()
        engine._offer_line.assert_called_once_with("Zabieraj stąd swoje dupsko.", False)

    def test_unpunctuated_single_glimpse_is_not_flushed(self):
        engine = self._engine()
        with patch("gamereader_engine.time.monotonic", return_value=30.00):
            engine._on_subtitle("Hey pie chcę ff")
        with patch("gamereader_engine.time.monotonic", return_value=30.60):
            engine._flush_candidate()
        engine._offer_line.assert_not_called()

    def test_frame_junk_before_known_line_is_cut(self):
        # sesja 15:15–15:17: napis z doklejonym z kadru przodem
        self.assertEqual(strip_known_prefix("Ubermach Tam! Tam! To moja łódź!", "Tam! Tam! To moja łódź!"),
                         "Tam! Tam! To moja łódź!")
        self.assertEqual(
            strip_known_prefix("są Andrese KRYSTEL Jeśli mam to zrobić, to musisz podjechać bliżej.",
                               "Jeśli mam to zrobić, to musisz podjechać bliżej."),
            "Jeśli mam to zrobić, to musisz podjechać bliżej.")
        # pierwsze zdanie kwestii to nie śmieć, nawet gdy pierwszy odczyt był ucięty
        self.assertIsNone(strip_known_prefix(
            "Dasz radę. Jak będzie gorąco, to w schowku jest klamna. Będę cię ostaniał.",
            "Jasz radę. Jak będzie gorąco, to w schowku jest klama. Będę cię"))

    def test_one_letter_polish_words_are_settled(self):
        self.assertFalse(ocr_reading_unsettled("O kurwa!"))
        self.assertFalse(ocr_reading_unsettled("O nie, to ty wybrałeś złą łajbę."))

    def test_exclamation_read_as_l_loses(self):
        self.assertGreater(ocr_reading_rank("Dobra! Idź, znajdź Jimmy'ego!"), ocr_reading_rank("Dobrał Idź, znajdź Jimmy'ego!"))
        self.assertGreater(ocr_reading_rank("Tylko nie silnik! Kurwa! Jebany silnik!"),
                           ocr_reading_rank("Tylko nie silnik! Kurwal Jebany silnik!"))

    def test_scigaj_gets_its_accent(self):
        self.assertEqual(repair_polish_ocr("Scigaj jacht."), "Ścigaj jacht.")

    def test_map_label_before_different_lines_is_learned(self):
        # sesja 15:33–15:36: „Lotnisko n …” (strefa z mapy) przed kolejnymi kwestiami — lektor to czytał
        lead = RecurringLead()
        seen = []
        for text in ("Lotnisko n Tato! Jezu! Pomocy!", "Lotnisko n Coś ci się popierdoliło, mały?",
                     "Lotnisko n Dobra. Dzięki.", "Lotniako i! Już nie.", "Lotnisko jest blisko."):
            lead.observe(text)
            seen.append(lead.strip(text))
        self.assertEqual(seen[2:], ["Dobra. Dzięki.", "Już nie.", "Lotnisko jest blisko."])
        self.assertEqual(lead.strip("Lotnisko n Patrzeć?"), "Patrzeć?")
        self.assertEqual(lead.strip("Lotnisko."), "")
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "lead.json"
            first = RecurringLead(store=store)
            for text in ("Lotnisko n Tato! Jezu!", "Lotnisko n Coś ci się popierdoliło, mały?"):
                first.observe(text)
            self.assertEqual(RecurringLead(store=store).strip("Lotnisko n Napraw samochód Amandy."),
                             "Napraw samochód Amandy.")
        # imię przed zdaniem bez przecinka raz się zdarza — nie jest śmieciem
        names = RecurringLead()
        names.observe("Franklin Chodź tu.")
        self.assertEqual(names.strip("Franklin Chodź tu."), "Franklin Chodź tu.")

    def test_hud_word_after_line_end_is_cut(self):
        self.assertEqual(trim_ocr_edges("Przypomnij mi, żebym nie przychodził do ciebie po porady rodzicielskie. ołzz"),
                         "Przypomnij mi, żebym nie przychodził do ciebie po porady rodzicielskie.")
        self.assertEqual(trim_ocr_edges("To mnie wykończy! Wiad"), "To mnie wykończy!")
        self.assertEqual(trim_ocr_edges("Idź. Już!"), "Idź. Już!")
        self.assertEqual(trim_ocr_edges("To już wszyscy? Dobra. Czas odbić łódź."), "To już wszyscy? Dobra. Czas odbić łódź.")
        self.assertEqual(trim_ocr_edges("Dość tego. Jasne? Dość."), "Dość tego. Jasne? Dość.")
        self.assertEqual(trim_ocr_edges("Kiepsko to brzmi. F ."), "Kiepsko to brzmi.")
        self.assertEqual(trim_ocr_edges("To już wszyscy? Dobra. Czas odbić łódź"), "To już wszyscy? Dobra. Czas odbić łódź")

    def test_short_word_inside_new_line_is_not_its_growth(self):
        # „Tata?”, potem „Nie nazywaj mnie tata, …” — lektor czytał tylko „małe ścierwo!…”
        self.assertFalse(extends_utterance("Tata?", "Nie nazywaj mnie tata, małe ścierwo! Lepiej, żeby nadal pływała."))
        self.assertTrue(extends_utterance("Złap Franklina", "Złap Franklina i jedź do garażu."))

    def test_mixed_case_junk_before_line_is_cut(self):
        # sesja 16:30: warianty śmieci syntezowały się na zapas i zabierały czas prawdziwej kwestii
        self.assertEqual(strip_lead_junk("pĘL Odpalamy te gablote."), "Odpalamy te gablote.")
        self.assertEqual(strip_lead_junk("TRici• Odpalamy te gablote."), "Odpalamy te gablote.")
        self.assertEqual(strip_lead_junk("OK Dobra, jedziemy."), "OK Dobra, jedziemy.")

    def test_camera_prompts_are_not_read(self):
        # sesja 16:31, warsztat w GTA: lektor czytał „Pierwsza osoba El ZOOM L RUSZAJ KAMERĄ”
        self.assertEqual(strip_hud_prompts("Pierwsza osoba El ZOOM L RUSZAJ KAMERĄ"), "")
        self.assertEqual(strip_hud_prompts("Dobra. Jak nówka. ZOOM L Pierwsza osoba ElE RUSZAJ KAMERĄ"), "Dobra. Jak nówka.")
        self.assertEqual(strip_hud_prompts("Zaokrąglisz trochę lasencję? Pierwsza osoba ElE"), "Zaokrąglisz trochę lasencję?")
        self.assertEqual(strip_hud_prompts("Wróć do domu, Franklin."), "Wróć do domu, Franklin.")

    def test_saved_band_that_sees_nothing_switches_to_auto(self):
        # sesja RDR2 17:09: ręczny pasek zapisany za wysoko — napisy były w automatycznym pasie niżej
        engine = Engine.__new__(Engine)
        engine.ps_window = (0, 33, 1512, 882)
        engine.region = (341, 588, 794, 226)
        engine.lock_region = True
        engine.game_regions = {"rdr2": {"region": [341, 588, 794, 226]}}
        engine._region_key = lambda: "rdr2"
        engine._source_kind = lambda: "ps"
        engine._band_profile = lambda: {"band": 0.18, "gap": 0.03, "inset": 0.10}
        engine._capture_region = lambda: engine.region
        engine.persist = Mock()
        engine.emit = Mock()
        engine.snapshot = lambda: {}
        engine.ocr = Mock()
        engine.ocr.read = lambda region: "Nie za ostro, bracie." if region[1] > 700 else ""
        self.assertTrue(engine._probe_auto_band())
        self.assertFalse(engine.lock_region)
        self.assertGreater(engine.region[1], 700)
        self.assertNotIn("rdr2", engine.game_regions)
        # w automatycznym pasie tylko menu — zostaje ręczny pasek
        engine.region, engine.lock_region = (341, 588, 794, 226), True
        engine.game_regions = {"rdr2": {"region": [341, 588, 794, 226]}}
        engine.ocr.read = lambda region: "Tryb fotograficzny WYBIERZ X WSTECZ" if region[1] > 700 else ""
        self.assertFalse(engine._probe_auto_band())
        self.assertTrue(engine.lock_region)

    def test_catchup_pace_stays_intelligible(self):
        # pace jak przy doganianiu z sesji 13:48 (boost 1,40, tts 0,92, żywy głos)
        speed, stretch = lektor_speed_split(0.53, "Zabierz Franklina blisko jachtu.")
        self.assertLessEqual(speed, LEKTOR_MAX_RATE)
        self.assertLessEqual(stretch, LEKTOR_MAX_STRETCH)
        self.assertLessEqual(speed * stretch, LEKTOR_MAX_RATE + 1e-6)
        # sesja 15:16: bełkot robiło atempo ×1,12 razem z szybkim modelem. Od 09.10 atempo ≤ ×1,10 tylko tam, gdzie
        # model jest sam ograniczony (krótki fragment, ≤1,02) — pomiar Parakeet: bez nowych błędów, nagrania −8 %
        self.assertLessEqual(stretch, 1.10 + 1e-6)
        self.assertLessEqual(speed * stretch, 1.25 + 1e-6)
        # długi fragment: tempo tylko w modelu, bez rozciągania ffmpeg
        long_speed, long_stretch = lektor_speed_split(0.53, "Zabierz Franklina blisko jachtu i wracaj do miasta.")
        self.assertEqual(long_stretch, 1.0)
        self.assertLessEqual(long_speed, LEKTOR_MAX_RATE)

    def test_stt_queue_drops_stale_audio_but_keeps_stop(self):
        from gamereader_engine import LiveTranscriber

        live = LiveTranscriber(None, lambda *_a: None)
        for i in range(8):
            live.submit(i)
        live.stop()
        live.submit(9)
        queued = []
        while True:
            try:
                queued.append(live.jobs.get_nowait())
            except Exception:
                break
        self.assertLessEqual(sum(item is not None for item in queued), 3)
        self.assertEqual(queued[-1], None)
        self.assertEqual(queued[-2], 9)

    def test_onnx_sessions_do_not_keep_an_arena(self):
        from gamereader_engine import _cap_onnx_arena

        seen = {}

        class Opts:
            enable_cpu_mem_arena = True
            enable_mem_pattern = True

        class Session:
            def __init__(self, path, sess_options=None, providers=None, **kwargs):
                seen["opts"] = sess_options

        class Ort:
            InferenceSession = Session

            def SessionOptions(self):
                return Opts()

        loader = type("Loader", (), {"ort": Ort()})()
        _cap_onnx_arena(loader)
        made = loader.ort.InferenceSession("m.onnx", sess_options=Opts(), providers=["CPU"])
        self.assertIsInstance(made, loader.ort.InferenceSession)
        self.assertIsInstance(made, Session)
        self.assertFalse(seen["opts"].enable_cpu_mem_arena)
        self.assertFalse(seen["opts"].enable_mem_pattern)


if __name__ == "__main__":
    unittest.main()
