from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice, build_default_safety_gate
from instrument_layer.safety import SafetyGateError
from instrument_layer.nanonis_device import InstrumentPreconditionError, InstrumentPostconditionError, NanonisCommandError


class ScanConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeNanonisBackend(scan_running=False)
        self.device = NanonisDevice(backend=self.backend)

    def frame(self, **overrides):
        return self.device.set_scan_frame(**(dict(center_x_m=0., center_y_m=0., width_m=1e-7,
                                                   height_m=2e-7, angle_deg=30.) | overrides))

    def test_frame_configuration_reads_back_without_motion_or_scan_start(self):
        frame = self.frame()
        self.assertAlmostEqual(frame.width_m, 1e-7)
        self.assertEqual(frame, self.device.get_scan_frame())
        names = [name for name, _ in self.backend.command_log]
        self.assertEqual(names.count("Scan_FrameSet"), 1)
        self.assertNotIn("Scan_Action", names)
        self.assertNotIn("FolMe_XYPosSet", names)

    def test_rotated_corners_outside_range_rejected_before_write(self):
        with self.assertRaises(SafetyGateError):
            self.frame(width_m=1.8e-6, height_m=1.8e-6, angle_deg=45.)
        self.assertNotIn("Scan_FrameSet", [name for name, _ in self.backend.command_log])

    def test_frame_requires_idle_and_valid_range(self):
        self.backend.scan_running = True
        with self.assertRaises(InstrumentPreconditionError): self.frame()
        self.backend.scan_running = False
        self.backend.piezo_range = (float("nan"), 2e-6, 4e-7)
        with self.assertRaises(ValueError): self.frame()

    def test_frame_resize_cannot_bypass_derived_speed_limits(self):
        self.backend.piezo_range = (10e-6, 10e-6, 400e-9)
        self.backend.scan_speed = (1e-5, 1e-5, .01, .01, 1, 1.)
        with self.assertRaises(SafetyGateError): self.frame(width_m=2e-6)
        self.assertNotIn("Scan_FrameSet", [name for name, _ in self.backend.command_log])

    def test_line_times_use_correct_set_get_modes_and_preserve_frame(self):
        frame = self.device.get_scan_frame()
        result = self.device.set_scan_line_times(forward_line_time_s=.5, backward_line_time_s=1.)
        args = next(args for name, args in self.backend.command_log if name == "Scan_SpeedSet")
        self.assertEqual(args[4], 2)
        self.assertEqual(result.keep_parameter_constant, 1)
        self.assertAlmostEqual(result.forward_m_per_s, frame.width_m/.5)
        self.assertEqual(self.device.get_scan_frame(), frame)
        with self.assertRaises(SafetyGateError):
            self.device.set_scan_line_times(forward_line_time_s=0, backward_line_time_s=1.)

    def test_speed_readback_failure_stops_without_retry(self):
        original = self.device.get_scan_speed()
        with patch.object(self.device, "get_scan_speed", return_value=original):
            with self.assertRaises(InstrumentPostconditionError):
                self.device.set_scan_line_times(forward_line_time_s=.6, backward_line_time_s=.6)
        names = [name for name, _ in self.backend.command_log]
        self.assertEqual(names.count("Scan_SpeedSet"), 1)
        self.assertIn("Scan_Action", names)

    def test_new_configuration_actions_stay_disabled_by_default(self):
        for name in ("scan_frame_set", "scan_speed_set"):
            self.assertFalse(build_default_safety_gate().get_action(name).live_enabled)


class LockInReadTests(unittest.TestCase):
    def test_assigned_phase_register_is_used_for_frequency(self):
        backend = FakeNanonisBackend(lock_in_phase_register=2)
        state = NanonisDevice(backend=backend).get_lock_in_modulator(1)
        self.assertEqual(state.signal.name, "Bias (V)")
        self.assertAlmostEqual(state.amplitude, .01)
        self.assertEqual(state.phase_register_frequency_hz, 823.)
        self.assertIn(("LockIn_ModPhasFreqGet", (2,)), backend.command_log)
        self.assertTrue(all("Set" not in name for name, _ in backend.command_log))

    def test_invalid_readback_and_vendor_error_are_not_valid_configuration(self):
        device = NanonisDevice(backend=FakeNanonisBackend(lock_in_amplitude=float("nan")))
        with self.assertRaises(NanonisCommandError): device.get_lock_in_modulator()
        with patch.object(device.backend, "LockIn_ModAmpGet", return_value=("unsupported", b"", [])):
            with self.assertRaises(NanonisCommandError): device.get_lock_in_modulator()
        with self.assertRaises(ValueError): device.get_lock_in_modulator(True)


if __name__ == "__main__": unittest.main()
