import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, PropertyMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from tools import nanonis_control as cli
from runtime_layer.control_requests import load_control_session, read_control_json, resolve_control_request
from workflow_layer.control_step import execute_control_step
from instrument_layer import FakeNanonisBackend, NanonisDevice, build_default_safety_gate


def session():
    return {'schema_version': 1, 'active_run_db': 'run.sqlite', 'authorization': 'authorization.json',
            'stop_file': 'control.stop', 'output_directory': 'results', 'purpose': 'offline test',
            'presets': {'pulse': {'action': 'apply_bias_pulse', 'arguments': {'target_bias_v': 1, 'duration_ms': 10}},
                        'move': {'action': 'move', 'arguments': {}},
                        'poke': {'action': 'poke', 'arguments': {'depth_nm': 1}},
                        'scan': {'action': 'scan', 'arguments': {'frame_time_s': 600}}}}


class ControlRequestTests(unittest.TestCase):
    def test_short_requests_keep_positions_units_and_reuse_only_fixed_parameters(self):
        saved = session()
        for compact, action, args in [
            ('pulse', 'apply_bias_pulse', {'target_bias_v': 1, 'duration_ms': 10}),
            (['move', 10, -20], 'move', {'position_nm': [10, -20]}),
            (['poke', 10, -20], 'poke', {'position_nm': [10, -20], 'depth_nm': 1}),
            (['poke', 10, -20, .5], 'poke', {'position_nm': [10, -20], 'depth_nm': .5}),
            (['scan', 10, -20, 20], 'scan', {'center_nm': [10, -20], 'size_nm': 20, 'frame_time_s': 600}),
            (['scan', 10, -20, 20, 300], 'scan', {'center_nm': [10, -20], 'size_nm': 20, 'frame_time_s': 300}),
        ]:
            with self.subTest(compact=compact):
                result = resolve_control_request(compact, saved)
                self.assertEqual(result, {'action': action, 'arguments': args, 'purpose': 'offline test'})
        self.assertEqual(saved, session())

    def test_ambiguous_or_incomplete_requests_are_rejected(self):
        for value in ['move', ['poke', 1], ['scan', 1, 2], ['move', True, 2],
                      ['scan', 1, 2, [20, 30]], ['scan', 1, 2, 20, 0], ['poke', 1, 2, -1],
                      ['move', float('nan'), 2], ['pulse', 2], {'preset': 'scan', 'arguments': {'size_nm': 20}},
                      {'preset': 'poke', 'arguments': {'position_nm': [1, 2], 'bias_v': 10}}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_control_request(value, session())

    def test_session_paths_resolve_locally_and_invalid_settings_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.json'
            path.write_text(json.dumps(session()))
            loaded = load_control_session(path)
            # Windows may expose the same temp directory through an 8.3 alias.
            self.assertEqual(loaded['stop_file'], str((Path(tmp) / 'control.stop').resolve()))
            for change in [{'schema_version': True}, {'port': 1.5}, {'timeout_s': -1}, {'host': ''},
                           {'presets': {'poke': {'action': 'move', 'arguments': {}}}},
                           {'presets': {'scan': {'action': 'scan', 'arguments': {'center_nm': [1, 2]}}}}]:
                path.write_text(json.dumps(session() | change))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_control_session(path)
            for malformed in ['{"preset":"pulse","preset":"poke"}', '{"value":NaN}']:
                path.write_text(malformed)
                with self.assertRaises(ValueError):
                    read_control_json(path)

    def test_dry_run_never_connects_or_opens_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.json'; path.write_text(json.dumps(session()))
            output = io.StringIO()
            with patch.object(cli, 'NanonisDevice') as connect, patch.object(cli, 'ExistingRunActionEventSink') as sink, \
                    patch.object(cli, 'load_control_authorization') as auth, contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['--session-file', str(path), 'step', 'scan', '10', '-20', '20', '--dry-run']), 0)
            connect.assert_not_called(); sink.assert_not_called(); auth.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['request']['arguments']['center_nm'], [10, -20])
            self.assertFalse((Path(tmp) / 'results').exists())

    def test_saved_pulse_executes_once_and_keeps_expanded_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.json'; path.write_text(json.dumps(session()))
            backend = FakeNanonisBackend(scan_running=False)
            device = NanonisDevice(backend=backend)
            output = io.StringIO()
            with patch.object(cli, 'NanonisDevice', return_value=device), patch.object(cli, 'ExistingRunActionEventSink'), \
                    patch.object(cli, 'load_control_authorization', return_value=build_default_safety_gate()), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['--session-file', str(path), 'step', 'pulse']), 0)
            self.assertEqual(sum(name == 'Bias_Pulse' for name, _ in backend.command_log), 1)
            receipt = json.loads(Path(json.loads(output.getvalue())['result_file']).read_text())
            self.assertEqual(receipt['request'], resolve_control_request('pulse', session()))
            self.assertEqual(receipt['status'], 'completed')


class QuickWorkflowTests(unittest.TestCase):
    def make_device(self, **kwargs):
        backend = FakeNanonisBackend()
        backend.scan_frame = (0., 0., 100e-9, 100e-9, 0.)
        device = NanonisDevice(backend=backend, **kwargs)
        return device, backend

    def run_quick(self, device, command):
        return execute_control_step(device, resolve_control_request(command, session()))

    def test_poke_stops_scan_moves_once_and_pokes_once_at_explicit_xy(self):
        events = []
        device, backend = self.make_device(action_event_sink=events.append)
        result = self.run_quick(device, ['poke', 10, 20])
        self.assertEqual([step['action'] for step in result['result']['steps']],
                         ['stop_scan', 'move_tip_xy', 'run_feedback_poke'])
        self.assertEqual(sum(name == 'FolMe_XYPosSet' for name, _ in backend.command_log), 1)
        self.assertEqual(sum(name == 'TipShaper_Start' for name, _ in backend.command_log), 1)
        self.assertAlmostEqual(device.get_follow_me_position().x_m, 10e-9)
        self.assertTrue(events)
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in backend.command_log])

    def test_square_scan_uses_frame_time_and_existing_matching_pixel_counts(self):
        device, backend = self.make_device()
        result = self.run_quick(device, ['scan', 10, 20, 20, 300])
        frame = device.get_scan_frame()
        self.assertAlmostEqual(frame.width_m, 20e-9)
        self.assertEqual(frame.height_m, frame.width_m)
        self.assertEqual(result['result']['frame_time_s'], 300)
        self.assertAlmostEqual(device.get_scan_speed().forward_line_time_s, 300 / 512)
        self.assertEqual(sum(name == 'Scan_FrameSet' for name, _ in backend.command_log), 1)
        self.assertTrue(device.get_scan_status().running)

    def test_preflight_rejects_invalid_target_depth_and_pixels_before_stopping_scan(self):
        for command in [['move', 1000, 0], ['poke', 0, 0, 1000], ['scan', 10000, 0, 20], ['scan', 0, 0, 20, .01]]:
            device, backend = self.make_device()
            with self.subTest(command=command), self.assertRaises((ValueError, RuntimeError)):
                self.run_quick(device, command)
            self.assertFalse(any(name in {'Scan_Action', 'FolMe_XYPosSet', 'TipShaper_Start', 'Scan_FrameSet'} for name, _ in backend.command_log))
        device, backend = self.make_device()
        backend.scan_buffer = (3, [0, 1, 2], 256, 128)
        with self.assertRaisesRegex(ValueError, 'equal existing'):
            self.run_quick(device, ['scan', 0, 0, 20])
        self.assertNotIn('Scan_Action', [name for name, _ in backend.command_log])

    def test_default_live_disabled_poke_does_not_stop_scan_or_move(self):
        device, backend = self.make_device()
        with patch.object(NanonisDevice, 'is_fake_backend', new_callable=PropertyMock, return_value=False), self.assertRaises(ValueError):
            self.run_quick(device, ['poke', 0, 0])
        self.assertNotIn('Scan_Action', [name for name, _ in backend.command_log])
        self.assertNotIn('FolMe_XYPosSet', [name for name, _ in backend.command_log])

    def test_operator_takeover_prevents_even_automatic_stop_and_later_poke(self):
        device, backend = self.make_device(stop_requested=lambda: True)
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_quick(device, ['poke', 0, 0])
        self.assertNotIn('Scan_Action', [name for name, _ in backend.command_log])
        stopped = [False]
        device, backend = self.make_device(stop_requested=lambda: stopped[0])
        original = device.move_tip_xy
        def move(*args):
            result = original(*args)
            stopped[0] = True
            return result
        with patch.object(device, 'move_tip_xy', side_effect=move), self.assertRaisesRegex(RuntimeError, 'cancelled') as error:
            self.run_quick(device, ['poke', 0, 0])
        self.assertEqual(len(error.exception.acquired_evidence['quick_step']['completed_steps']), 2)
        self.assertNotIn('TipShaper_Start', [name for name, _ in backend.command_log])
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in backend.command_log])

    def test_poke_error_keeps_completed_move_without_retry_or_cleanup(self):
        device, backend = self.make_device()
        with patch.object(device, 'run_feedback_poke', side_effect=OSError('response unknown')) as poke, self.assertRaises(OSError) as error:
            self.run_quick(device, ['poke', 0, 0])
        poke.assert_called_once()
        self.assertEqual([x['action'] for x in error.exception.acquired_evidence['quick_step']['completed_steps']], ['stop_scan', 'move_tip_xy'])
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in backend.command_log])


if __name__ == '__main__':
    unittest.main()
