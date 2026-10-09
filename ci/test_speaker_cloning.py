"""Zbieranie próbek, przypisanie dialogu i izolacja głosów; bez pobierania modeli."""
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge


class SpeakerBankTest(unittest.TestCase):
    def setUp(self):
        # testy podają sztuczne czasy (10, 12…) i płaskie nagrania z krótkim „tekstem” — luzujemy zegar i bramkę jakości
        for name, value in (("SPEAKER_MATCH_TTL", 1e9), ("SPEAKER_SAMPLE_MIN_WORDS", 1), ("SPEAKER_SAMPLE_MIN_VOICED", 0.0),
                            ("SPEAKER_SAMPLE_SEC", (4.5, 6.5))):
            patcher = patch.object(ge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cloud = SimpleNamespace(ready=True, embed=Mock(return_value=np.array([1., 0.])), set_voice=Mock(return_value=0.0),
                                     forget_voice=Mock())
        self.transcribe = Mock(return_value="Reference speech.")
        self.bank = ge.SpeakerBank(lambda: self.cloud, self.transcribe)
        self.bank._backend = self.cloud

    def clip(self, seconds=1.6):
        return np.full(int(ge.SAMPLE_RATE * seconds), .1, dtype=np.float32)

    def observe(self, start, seconds=1.6, provisional=False):
        self.bank._handle(self.clip(seconds), start, start + seconds, provisional)

    def test_collects_sample_then_switches_to_character_voice(self):
        self.observe(10)
        self.assertIsNone(self.bank.voice_for(10.1))
        self.observe(12)
        self.assertFalse(self.bank.any_ready())
        self.observe(14)
        self.assertTrue(self.bank.any_ready())
        key = self.bank.voice_for(14.1)
        self.assertIsNotNone(key)
        self.assertEqual(self.cloud.set_voice.call_args.args[0], key)
        self.assertGreaterEqual(self.transcribe.call_args.args[0].size / ge.SAMPLE_RATE, 4.5)
        self.assertLessEqual(self.transcribe.call_args.args[0].size / ge.SAMPLE_RATE, 6.5)
        self.assertEqual(self.bank.speakers[key]["pool"], [])

    def test_two_speakers_get_separate_samples_and_voices(self):
        self.observe(10, 5)
        first = self.bank.voice_for(10.1)
        self.cloud.embed.return_value = np.array([0., 1.])
        self.observe(20, 5)
        second = self.bank.voice_for(20.1)
        self.assertNotEqual(first, second)
        self.assertIsNotNone(second)
        self.assertEqual(self.bank.voice_for(10.1), first)
        self.assertIsNone(self.bank.voice_for(30))

    def test_provisional_does_not_create_or_collect_twice(self):
        self.observe(10, 1.2, True)
        self.assertEqual(len(self.bank.speakers), 0)
        self.observe(10, 5)
        self.assertEqual(len(self.bank.timeline), 1)
        self.observe(20, 1.2, True)
        self.assertEqual(self.bank.voice_for(20.1), self.bank.voice_for(10.1))
        self.assertEqual(self.cloud.set_voice.call_count, 1)

    def test_long_utterance_is_capped_before_transcribing(self):
        self.observe(10, 18)
        self.assertEqual(self.transcribe.call_args.args[0].size, int(6.5 * ge.SAMPLE_RATE))

    def test_invalid_embedding_never_becomes_a_character(self):
        for value in ([np.nan, 0.], [0., 0.], [np.inf, 1.]):
            self.cloud.embed.return_value = np.array(value)
            self.observe(10, 5)
        self.assertEqual(self.bank.speakers, {})
        self.transcribe.assert_not_called()

    def test_backend_replacement_invalidates_ready_voices(self):
        self.observe(10, 5)
        self.assertTrue(self.bank.any_ready())
        self.cloud = SimpleNamespace(ready=True, embed=Mock(return_value=np.array([1., 0.])), set_voice=Mock(return_value=0.0))
        self.assertFalse(self.bank.any_ready())
        self.assertIsNone(self.bank.voice_for(10.1))
        self.bank.observe(self.clip(5), 20, 25)
        self.bank._jobs.join()
        self.assertIsNone(self.bank.voice_for(10.1))
        self.assertIsNotNone(self.bank.voice_for(20.1))
        self.bank.close()

    def test_reset_during_transcription_discards_old_sample(self):
        self.transcribe.side_effect = lambda _: (self.bank.reset() or "Reference speech.")
        self.observe(10, 5)
        self.cloud.set_voice.assert_not_called()
        self.assertEqual(self.bank.speakers, {})

    def test_nearby_next_speaker_does_not_replace_speaker_at_subtitle_time(self):
        self.observe(10, 5)
        first = self.bank.voice_for(14.8)
        self.cloud.embed.return_value = np.array([0., 1.])
        self.observe(15.1, 5)
        self.assertEqual(self.bank.voice_for(14.8), first)
        self.assertNotEqual(self.bank.voice_for(15.2), first)

    def test_reset_allocates_a_different_voice_key(self):
        self.observe(10, 5)
        first = self.bank.voice_for(10.1)
        self.bank.reset()
        self.observe(20, 5)
        self.assertNotEqual(first, self.bank.voice_for(20.1))

    def test_empty_transcript_can_retry_on_next_utterance(self):
        # każdy segment jest transkrybowany raz do osi czasu, a próbka drugi raz — stąd po dwa wywołania na wypowiedź
        self.transcribe.side_effect = ["", "", "Reference speech.", "Reference speech."]
        self.observe(10, 5)
        self.assertFalse(self.bank.any_ready())
        self.observe(20, 2)
        self.assertTrue(self.bank.any_ready())


class CaptureAndQueueTest(unittest.TestCase):
    def test_cutter_keeps_same_start_for_provisional_and_final_and_trims_silence(self):
        cutter = ge.UtteranceCutter()
        cutter._voice = lambda audio: bool(audio[0])
        frame = np.ones(int(.2 * ge.SAMPLE_RATE), dtype=np.float32)
        with patch.object(ge.time, "monotonic", return_value=10.):
            for _ in range(6):
                cutter.feed(frame)
        sample, start = cutter.peek(1.)
        self.assertIsNone(cutter.peek(1.))
        result = None
        for _ in range(int(ge.SILENCE_SEC / .2) + 2):
            result = cutter.feed(np.zeros_like(frame))
            if result is not None:
                break
        self.assertIsNotNone(result)
        self.assertEqual(cutter.last_start, start)
        self.assertAlmostEqual(cutter.last_end - start, 1.2)
        self.assertEqual(result.size, sample.size)

    def test_cloning_keeps_different_subtitles_in_separate_turns(self):
        engine = ge.Engine.__new__(ge.Engine)
        engine._queue = [("Pierwsza kwestia.", False, "Pierwsza kwestia."), ("Druga kwestia.", False, "Druga kwestia.")]
        engine.pending_lock = threading.Lock()
        engine.has_pending = threading.Event()
        engine._prefetch_parts = None
        engine.speakers = object()
        engine.lektor = SimpleNamespace(cloud=object())
        self.assertEqual(engine._waiting_parts()[2], ["Pierwsza kwestia."])
        self.assertEqual(engine._take_pending()[3], ["Pierwsza kwestia."])
        self.assertEqual(engine._take_pending()[3], ["Druga kwestia."])

    def test_stt_preserves_recording_time_through_transcription(self):
        stt = SimpleNamespace(ensure=lambda: None, warmup=lambda: None, transcribe=lambda _: "Come here, please.")
        callback = Mock()
        live = ge.LiveTranscriber(stt, callback)
        audio = np.zeros(16000, dtype=np.float32)
        live.submit(audio, start=12.3)
        live.stop()
        live._run()
        callback.assert_called_once_with("Come here, please.", audio, 12.3)


class VoiceCacheTest(unittest.TestCase):
    def test_same_character_key_in_two_sessions_does_not_reuse_audio(self):
        lektor = ge.MaleLektor.__new__(ge.MaleLektor)
        lektor.ensure = lambda: None
        lektor.voice = "M5"
        lektor.tts_scale = 1.
        lektor.ffmpeg = None
        cloud = SimpleNamespace(tag="ov1|pack", failed_until=0., voice_rev={"s1": 1}, voice_session="first")
        lektor.cloud = cloud
        lektor._synth_cloud = Mock(return_value=1.)

        def master(raw, path, *_):
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(24000)
                handle.writeframes(b"\0" * 2400)

        lektor._master = master
        with tempfile.TemporaryDirectory() as directory, patch.object(ge, "CACHE_DIR", Path(directory)), patch.object(ge, "log_timing", lambda _: None):
            first = lektor.prepare("Cześć.", voice="s1")
            self.assertEqual(first, lektor.prepare("Cześć.", voice="s1"))
            self.assertEqual(lektor._synth_cloud.call_count, 1)
            cloud.voice_session = "second"
            second = lektor.prepare("Cześć.", voice="s1")
            self.assertNotEqual(first, second)
            self.assertEqual(lektor._synth_cloud.call_count, 2)


if __name__ == "__main__":
    unittest.main()
