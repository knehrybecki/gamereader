"""18.11: lektor nie szedł na słuchawki Bluetooth — odtwarzacz otwierał wyjście raz przy starcie i zostawał
na głośnikach MacBooka. Teraz wyjście da się wybrać w menu, a „Systemowe” idzie za domyślnym wyjściem Maca."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gamereader_engine as ge
from gamereader_engine import Engine, PlayerProcess, output_device_names, pick_output_device

DEVICES = [
    {"name": "Fresh 'n Rebel 3AGH301", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "Fresh 'n Rebel 3AGH301", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "DualSense Wireless Controller", "max_input_channels": 0, "max_output_channels": 4},
    {"name": "Mikrofon (MacBook Pro)", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "Głośniki (MacBook Pro)", "max_input_channels": 0, "max_output_channels": 2},
]


class OutputListTest(unittest.TestCase):
    def test_only_outputs_are_listed_once(self):
        self.assertEqual(
            output_device_names(DEVICES),
            ["Fresh 'n Rebel 3AGH301", "DualSense Wireless Controller", "Głośniki (MacBook Pro)"],
        )

    def test_chosen_device_is_found_by_name(self):
        self.assertEqual(pick_output_device("Głośniki (MacBook Pro)", DEVICES), 4)
        self.assertEqual(pick_output_device("Fresh 'n Rebel 3AGH301", DEVICES), 1)  # wyjście, nie mikrofon

    def test_system_or_missing_device_means_default(self):
        self.assertIsNone(pick_output_device("", DEVICES))
        self.assertIsNone(pick_output_device("AirPods", DEVICES))  # odłączone — gra domyślne wyjście


class PlayerDeviceTest(unittest.TestCase):
    def test_device_goes_to_new_player_process(self):
        player = PlayerProcess()
        player.set_device("Głośniki (MacBook Pro)")
        fake = Mock()
        fake.stdout.readline.return_value = "READY\n"
        fake.stdout.__iter__ = lambda _self: iter([])
        with patch.object(ge.subprocess, "Popen", return_value=fake) as popen:
            player._start()
        self.assertEqual(popen.call_args.args[0][-1], "Głośniki (MacBook Pro)")

    def test_running_player_switches_output(self):
        player = PlayerProcess()
        player.proc = Mock()
        player.proc.poll.return_value = None
        player.set_device("DualSense Wireless Controller")
        player.proc.stdin.write.assert_called_with("DEVICE DualSense Wireless Controller\n")
        player.set_device("")
        player.proc.stdin.write.assert_called_with("DEVICE \n")


class EngineOutputTest(unittest.TestCase):
    def test_menu_choice_reaches_player_and_config(self):
        engine = Engine.__new__(Engine)
        engine.lektor = Mock()
        engine.output_device = ""
        engine.outputs = []
        engine.persist = Mock()
        engine.emit = Mock()
        engine.set_output_device("DualSense Wireless Controller")
        engine.lektor.out.set_device.assert_called_with("DualSense Wireless Controller")
        self.assertEqual(engine.output_device, "DualSense Wireless Controller")


if __name__ == "__main__":
    unittest.main()
