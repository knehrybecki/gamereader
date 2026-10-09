"""Wypowiedź z kilkoma osobami jest dzielona po momentach pojawiania się napisów (test 09.10: 7,8 s = 3 postacie)."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge

SR = ge.SAMPLE_RATE


def make_bank():
    # atrapa modelu: „mówcę” rozpoznaje po poziomie nagrania (0,1 → postać A, 0,3 → postać B)
    def embed(audio):
        return np.array([1.0, 0.0], dtype=np.float32) if float(np.mean(np.abs(audio))) < 0.2 else np.array([0.0, 1.0], dtype=np.float32)

    cloud = SimpleNamespace(ready=True, embed=embed, set_voice=lambda *a: 0.0)
    bank = ge.SpeakerBank(lambda: cloud, lambda a: "Reference speech.")
    bank._backend = cloud
    return bank


def two_speakers(seconds_a=3.0, seconds_b=3.0):
    return np.concatenate([np.full(int(SR * seconds_a), 0.1, np.float32), np.full(int(SR * seconds_b), 0.3, np.float32)])


class SplitBySubtitlesTest(unittest.TestCase):
    def test_a_new_subtitle_in_the_middle_splits_the_utterance(self):
        bank = make_bank()
        audio = two_speakers()
        end = 100.0; begin = end - audio.size / SR
        bank.note_subtitle(begin + 0.2)   # pierwszy napis (początek wypowiedzi)
        bank.note_subtitle(begin + 3.2)   # drugi napis: odpowiada druga postać
        pieces = bank.split_by_subtitles(audio, begin + 0.25, end)
        self.assertEqual(len(pieces), 2)
        self.assertAlmostEqual(pieces[0][2], begin + 3.2 - ge.SPEAKER_ANCHOR_LEAD, delta=0.01)
        self.assertLess(float(np.mean(np.abs(pieces[0][0]))), 0.2)
        self.assertGreater(float(np.mean(np.abs(pieces[1][0]))), 0.2)

    def test_two_speakers_get_two_characters(self):
        bank = make_bank()
        audio = two_speakers()
        end = 100.0; begin = end - audio.size / SR
        bank.note_subtitle(begin + 0.2); bank.note_subtitle(begin + 3.2)
        for clip, a, b in bank.split_by_subtitles(audio, begin + 0.25, end):
            bank._handle(clip, a, b, False)
        keys = {e["key"] for e in bank.timeline}
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(bank.speakers), 2)

    def test_without_a_second_subtitle_nothing_is_cut(self):
        bank = make_bank()
        audio = two_speakers()
        end = 100.0; begin = end - audio.size / SR
        bank.note_subtitle(begin + 0.2)
        self.assertEqual(len(bank.split_by_subtitles(audio, begin + 0.25, end)), 1)

    def test_flickering_ocr_variants_do_not_create_extra_cuts(self):
        bank = make_bank()
        bank.note_subtitle(10.0); bank.note_subtitle(10.4); bank.note_subtitle(10.9)
        self.assertEqual(list(bank.anchors), [10.0])

    def test_anchors_near_the_edges_are_ignored(self):
        bank = make_bank()
        audio = two_speakers()
        end = 100.0; begin = end - audio.size / SR
        bank.note_subtitle(begin + 0.3); bank.note_subtitle(end - 0.3)
        self.assertEqual(len(bank.split_by_subtitles(audio, begin, end)), 1)


if __name__ == "__main__":
    unittest.main()


class PolishSampleRejectedTest(unittest.TestCase):
    def _bank(self, transcript, english=True):
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.array([1.0, 0.0], dtype=np.float32), set_voice=lambda *a: 0.0)
        bank = ge.SpeakerBank(lambda: cloud, lambda a: transcript, expects_english=lambda: english)
        bank._backend = cloud
        return bank, cloud

    def _feed(self, bank, n=4):
        tt = np.arange(int(SR * 1.8)) / SR
        speech = (0.2 * np.sin(2 * np.pi * 140 * tt) * (1 + np.sin(2 * np.pi * 4 * tt)) / 2).astype(np.float32)
        for i in range(n):
            bank._handle(speech, 10.0 + 3 * i, 11.8 + 3 * i, False)

    def test_sample_that_sounds_polish_is_not_cloned(self):
        bank, cloud = self._bank("Dobrze w nich wyglądasz. Mm-hmm.")
        self._feed(bank)
        self.assertFalse(bank.any_ready())
        self.assertTrue(next(iter(bank.speakers.values()))["blocked"])

    def test_english_sample_is_cloned(self):
        bank, cloud = self._bank("Woo! Looking good! Hey, wouldn't you two mind driving us down there?")
        self._feed(bank)
        self.assertTrue(bank.any_ready())

    def test_polish_dubbed_game_may_clone_polish_speech(self):
        bank, cloud = self._bank("Dobrze w nich wyglądasz, kochanie. Naprawdę dobrze, mówię ci.", english=False)
        self._feed(bank)
        self.assertTrue(bank.any_ready())


class NoisyShortSegmentsTest(unittest.TestCase):
    def test_short_noisy_segments_do_not_create_characters(self):
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.array([0.0, 1.0], dtype=np.float32), set_voice=lambda *a: 0.0)
        bank = ge.SpeakerBank(lambda: cloud, lambda a: "Hello.")
        bank._backend = cloud
        bank.speakers["s1_x_1"] = {"centroid": np.array([1.0, 0.0], dtype=np.float32), "n": 5.0, "pool": [], "ready": False}
        for i in range(5):  # krótkie odcinki z wektorem daleko od jedynej postaci
            bank._handle(np.full(int(SR * 1.0), 0.1, np.float32), 10.0 + 3 * i, 11.0 + 3 * i, False)
        self.assertEqual(len(bank.speakers), 1)

    def test_long_segment_far_from_everyone_creates_one(self):
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.array([0.0, 1.0], dtype=np.float32), set_voice=lambda *a: 0.0)
        bank = ge.SpeakerBank(lambda: cloud, lambda a: "Hello.")
        bank._backend = cloud
        bank.speakers["s1_x_1"] = {"centroid": np.array([1.0, 0.0], dtype=np.float32), "n": 5.0, "pool": [], "ready": False}
        bank._handle(np.full(int(SR * 2.5), 0.1, np.float32), 10.0, 12.5, False)
        self.assertEqual(len(bank.speakers), 2)

    def test_short_segment_close_to_a_character_is_matched(self):
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.array([0.8, 0.6], dtype=np.float32), set_voice=lambda *a: 0.0)
        bank = ge.SpeakerBank(lambda: cloud, lambda a: "Hello.")
        bank._backend = cloud
        bank.speakers["s1_x_1"] = {"centroid": np.array([1.0, 0.0], dtype=np.float32), "n": 5.0, "pool": [], "ready": False}
        bank._handle(np.full(int(SR * 1.0), 0.1, np.float32), 10.0, 11.0, False)
        self.assertEqual(bank.timeline[-1]["key"], "s1_x_1")


class EnoughVoicesTest(unittest.TestCase):
    def _bank(self, vec):
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.asarray(vec, dtype=np.float32), set_voice=lambda *a: 0.0)
        bank = ge.SpeakerBank(lambda: cloud, lambda a: "Hello there.")
        bank._backend = cloud
        for i in range(ge.SPEAKER_READY_ENOUGH):
            v = np.zeros(8, dtype=np.float32); v[i] = 1.0
            bank.speakers[f"s{i+1}_x_1"] = {"centroid": v, "n": 5.0, "pool": [], "ready": True}
        return bank

    def test_with_enough_voices_a_new_segment_joins_the_closest_ready_one(self):
        v = np.zeros(8, dtype=np.float32); v[0] = 0.75; v[7] = 0.66   # dalej niż próg nowej postaci, blisko s1
        bank = self._bank(v / np.linalg.norm(v))
        bank._handle(np.full(int(SR * 2.5), 0.1, np.float32), 10.0, 12.5, False)
        self.assertEqual(len(bank.speakers), ge.SPEAKER_READY_ENOUGH)
        self.assertEqual(bank.timeline[-1]["key"], "s1_x_1")

    def test_with_enough_voices_a_far_segment_creates_nothing(self):
        v = np.zeros(8, dtype=np.float32); v[7] = 1.0
        bank = self._bank(v)
        bank._handle(np.full(int(SR * 2.5), 0.1, np.float32), 10.0, 12.5, False)
        self.assertEqual(len(bank.speakers), ge.SPEAKER_READY_ENOUGH)
        self.assertIsNone(bank.timeline[-1]["key"])

    def test_ready_speaker_is_never_sampled_again(self):
        bank = self._bank(np.eye(8, dtype=np.float32)[0])
        bank._collect = Mock = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        bank._handle(np.full(int(SR * 2.5), 0.1, np.float32), 10.0, 12.5, False)
        # segment przypisany do gotowej postaci wchodzi do _collect, ale ta od razu wraca przy sp["ready"]
        sp = bank.speakers["s1_x_1"]
        self.assertEqual(sp["pool"], [])


class SampleQualityTest(unittest.TestCase):
    def _speech(self, seconds=4.0):
        t = np.arange(int(SR * seconds)) / SR
        return (0.2 * np.sin(2 * np.pi * 140 * t) * (1 + np.sin(2 * np.pi * 4 * t)) / 2).astype(np.float32)

    def test_a_grunt_or_a_few_words_is_rejected(self):
        for text in ("Mm-hmm.", "Come on.", "Come on, get him.", "Yeah. Okay."):
            self.assertTrue(ge.SpeakerBank._sample_quality(self._speech(), text)[1], text)

    def test_real_speech_is_accepted(self):
        quality, why = ge.SpeakerBank._sample_quality(self._speech(), "Hey, wouldn't you two mind driving us down there?")
        self.assertEqual(why, "")
        self.assertGreater(quality, 0)

    def test_silence_is_rejected(self):
        self.assertTrue(ge.SpeakerBank._sample_quality(np.zeros(SR * 4, np.float32), "I look good in these, honestly.")[1])

    def test_weak_sample_does_not_create_a_voice_and_collection_restarts(self):
        calls = []
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.array([1.0, 0.0], dtype=np.float32),
                                set_voice=lambda *a: calls.append(a) or 0.0, forget_voice=lambda k: None)
        bank = ge.SpeakerBank(lambda: cloud, lambda a: "Mm-hmm.")
        bank._backend = cloud
        for i in range(4):
            bank._handle(self._speech(1.8), 10.0 + 3 * i, 11.8 + 3 * i, False)
        self.assertEqual(calls, [])
        self.assertFalse(bank.any_ready())
        sp = next(iter(bank.speakers.values()))
        self.assertGreaterEqual(sp["tries"], 1)

    def test_outlier_clip_does_not_enter_the_sample(self):
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.array([1.0, 0.0], dtype=np.float32),
                                set_voice=lambda *a: 0.0, forget_voice=lambda k: None)
        bank = ge.SpeakerBank(lambda: cloud, lambda a: "x")
        bank._backend = cloud
        bank.speakers["s1_x_1"] = {"centroid": np.array([1.0, 0.0], dtype=np.float32), "n": 5.0, "pool": [], "ready": False}
        bank._collect("s1_x_1", self._speech(2.0), 2.0, cloud, bank._generation, np.array([0.0, 1.0], dtype=np.float32))
        self.assertEqual(bank.speakers["s1_x_1"]["pool"], [])


class PruneVoicesTest(unittest.TestCase):
    def _bank(self):
        forgotten = []
        cloud = SimpleNamespace(ready=True, embed=lambda a: np.array([1.0, 0.0]), set_voice=lambda *a: 0.0,
                                forget_voice=lambda k: forgotten.append(k))
        bank = ge.SpeakerBank(lambda: cloud, lambda a: "x")
        bank._backend = cloud
        return bank, cloud, forgotten

    def _add(self, bank, key, vec, quality, used=0):
        v = np.asarray(vec, dtype=np.float32)
        bank.speakers[key] = {"centroid": v / np.linalg.norm(v), "n": 5.0, "pool": [], "ready": True, "quality": quality, "used": used}

    def test_duplicate_voice_keeps_the_better_sample(self):
        bank, cloud, forgotten = self._bank()
        self._add(bank, "a", [1, 0, 0], quality=8.0)
        self._add(bank, "b", [1, 0.1, 0], quality=3.0)
        bank._prune("b", cloud)
        self.assertEqual(forgotten, ["b"])
        self.assertFalse(bank.speakers["b"]["ready"])
        self.assertEqual(bank.speakers["b"]["alias"], "a")

    def test_distinct_voices_are_both_kept(self):
        bank, cloud, forgotten = self._bank()
        self._add(bank, "a", [1, 0, 0], quality=8.0)
        self._add(bank, "b", [0, 1, 0], quality=3.0)
        bank._prune("b", cloud)
        self.assertEqual(forgotten, [])

    def test_voices_above_the_limit_drop_the_least_used(self):
        bank, cloud, forgotten = self._bank()
        for i in range(ge.SPEAKER_KEEP + 1):
            v = [0.0] * (ge.SPEAKER_KEEP + 1); v[i] = 1.0
            self._add(bank, f"v{i}", v, quality=5.0, used=10 if i else 0)
        bank._prune(f"v{ge.SPEAKER_KEEP}", cloud)
        self.assertEqual(forgotten, ["v0"])
        self.assertNotEqual(bank.speakers["v0"]["alias"], "v0")
