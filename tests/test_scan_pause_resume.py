from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice, build_default_safety_gate
from instrument_layer.nanonis_device import InstrumentPreconditionError, InstrumentPostconditionError


class ScanPauseResumeTests(unittest.TestCase):
    def test_resume_uses_native_resume_without_restarting_or_clearing_buffer(self):
        backend = FakeNanonisBackend(scan_running=True, scan_direction_up=False)
        device = NanonisDevice(backend=backend)
        original_data = backend.frame_data
        paused = device.pause_scan()
        self.assertFalse(paused['status'].running)
        resumed = device.resume_scan()
        self.assertTrue(resumed['status'].running)
        self.assertEqual([args for name, args in backend.command_log if name == 'Scan_Action'],
                         [(2, 0), (3, 0)])
        self.assertEqual(backend.frame_data, original_data)

    def test_unowned_stopped_or_changed_scan_cannot_resume(self):
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        with self.assertRaises(InstrumentPreconditionError): device.resume_scan()
        backend.scan_running = True
        device.pause_scan()
        backend.scan_frame = (0., 0., 2e-7, 2e-7, 0.)
        with self.assertRaises(InstrumentPreconditionError): device.resume_scan()
        self.assertFalse(any(args[0] == 3 for name, args in backend.command_log if name == 'Scan_Action'))

    def test_stop_invalidates_pause_and_failed_pause_stops_without_withdraw(self):
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)
        device.pause_scan()
        device.stop_scan()
        with self.assertRaises(InstrumentPreconditionError): device.resume_scan()
        backend.scan_running = True
        with patch.object(backend, 'Scan_StatusGet', side_effect=[(1,), (2,)]):
            with self.assertRaises(InstrumentPostconditionError): device.pause_scan()
        self.assertEqual([args[0] for name, args in backend.command_log if name == 'Scan_Action'], [2,1,2,1])
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in backend.command_log])

    def test_resume_requires_feedback_and_defaults_remain_live_disabled(self):
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)
        device.pause_scan()
        backend.z_controller_on = False
        with self.assertRaises(InstrumentPreconditionError): device.resume_scan()
        for name in ('scan_pause', 'scan_resume'):
            self.assertFalse(build_default_safety_gate().get_action(name).live_enabled)


if __name__ == '__main__': unittest.main()
