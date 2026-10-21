"""Recorded Tip Shaper examples, competing waveforms, read-only integration."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from analysis_layer.tip_shaper_recognition import recognize_tip_shaper
from analysis_layer.recording_review import tip_shaper_observation_context
from data_layer import RunStore, TelemetrySample
from data_layer.recording_signals import iter_recording_signals
from instrument_layer import ActionEventEmitter, ActionName, FakeNanonisBackend, NanonisDevice, NanonisCommandError
from knowledge_base.recording_pattern import load_tip_shaper_recording_pattern
from workflow_layer.action_audit import ExternalStateObserver
from workflow_layer.recording_review import publish_recording_review
from workflow_layer import TelemetryRecorder


PATTERN = load_tip_shaper_recording_pattern()
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/tip_shaper_recorded.json').read_text(encoding='utf-8'))


def waveform(parameters=None):
    p = parameters or PATTERN['parameters']
    down, dwell, up = p['lift_time_1_s'], p['bias_settling_time_s'], p['lift_time_2_s']
    total = down + dwell + up
    rows = []
    for seq, t in enumerate(np.arange(0, 6, .02)):
        phase = t - 1
        dz = np.interp(phase, [-1, 0, down, down+dwell, total, total+.1, total+.12, 5],
                       [0, 0, p['tip_lift_m'], p['tip_lift_m'], p['tip_lift_m']+p['lift_height_m'],
                        p['tip_lift_m']+p['lift_height_m'], 0, 0])
        current = 1e-10 if phase < .08 or phase > down+dwell+.04 else 3e-9
        tick = 100_000_000_000 + round(t*1e9)
        rows.append([seq, 0, tick, 1_700_000_000_000_000_000+tick, current, 0., 0., 10e-9+dz])
    return rows


def append_signals(store, rows):
    for seq, segment, tick, utc, current, x, y, z in rows:
        store.append_sample(TelemetrySample(seq, segment, tick, utc, tick, tick, current, x, y, z))


class TipShaperRecognitionTests(unittest.TestCase):
    def test_two_anonymized_recorded_operations_are_recognized_without_event_labels(self):
        result = recognize_tip_shaper(iter(FIXTURE['signals']), PATTERN)
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['detections']), 2)
        for event, expected in zip(result['detections'], FIXTURE['expected_onset_utc_ns']):
            self.assertLess(abs(event['start_utc_ns']-expected), 50_000_000)
            self.assertGreater(event['peak_current_ratio'], 80)
            self.assertTrue(event['possible_current_clipping'])
            self.assertEqual(event['parameter_source'], 'waveform_inferred')
            self.assertNotIn('bias_v', event['matched_parameters'])
            self.assertAlmostEqual(event['observed_z_return_m'] / 1e-9, 1.0, delta=.03)
            self.assertEqual(event['actor'], 'unknown')
            self.assertEqual(event['outcome'], 'not_assessed')

    def test_configuration_change_drives_recognition_without_changing_the_template(self):
        p = PATTERN['parameters'] | {'tip_lift_m': -1.4e-9, 'lift_height_m': 2e-9,
                                     'lift_time_1_s': .3, 'lift_time_2_s': .5}
        rows = waveform(p)
        self.assertEqual(recognize_tip_shaper(rows, PATTERN)['detections'], [])
        before = {'monotonic_ns': rows[0][2], 'available': True, 'parameters': p}
        after = before | {'monotonic_ns': rows[-1][2], 'parameters': PATTERN['parameters']}
        events = recognize_tip_shaper(rows, PATTERN, parameter_snapshots=[after, before])['detections']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['matched_parameters']['tip_lift_m'], -1.4e-9)
        self.assertEqual(events[0]['parameter_source'], 'recorded_configuration')
        self.assertGreater(events[0]['parameter_snapshot_age_s'], 0)
        self.assertFalse(events[0]['parameters_are_execution_proof'])
        self.assertAlmostEqual(events[0]['observed_z_return_m'] / 1e-9, 2., delta=.03)
        self.assertIn('post_return', events[0])
        self.assertEqual(recognize_tip_shaper(rows, PATTERN, parameter_snapshots=[after])['detections'], [])

    def test_unavailable_parameters_fall_back_and_nonmatching_signals_are_not_pokes(self):
        rows = waveform()
        unavailable = {'monotonic_ns': rows[0][2], 'available': False, 'parameters': None}
        self.assertEqual(len(recognize_tip_shaper(rows, PATTERN, parameter_snapshots=[unavailable])['detections']), 1)
        for kind in ('current_spike_only', 'withdrawal', 'scan_motion', 'flat_current', 'incomplete_return', 'gap', 'nan'):
            changed = [row.copy() for row in rows]
            for row in changed:
                if kind == 'current_spike_only': row[7] = 10e-9
                if kind == 'withdrawal': row[7] = 10e-9 + max(0., (row[2]-101_000_000_000)/1e9)*1e-9
                if kind == 'scan_motion': row[5] = row[0]*.2e-9
                if kind == 'flat_current': row[4] = 1e-10
                if kind == 'incomplete_return' and row[0] >= 70: row[7] = 9.4e-9
                if kind == 'nan' and row[0] == 70: row[7] = float('nan')
            if kind == 'gap': changed = [row for row in changed if not 65 <= row[0] <= 72]
            with self.subTest(kind=kind):
                self.assertEqual(recognize_tip_shaper(changed, PATTERN)['detections'], [])

    def test_budget_and_segment_boundaries_do_not_fabricate_complete_events(self):
        rows = waveform()
        result = recognize_tip_shaper(iter(rows), PATTERN, maximum_samples=60)
        self.assertFalse(result['complete'])
        self.assertEqual(result['examined_samples'], 60)
        self.assertEqual(result['detections'], [])
        for row in rows[70:]: row[1] = 1
        self.assertEqual(recognize_tip_shaper(rows, PATTERN)['detections'], [])

    def test_published_review_contains_automatic_results_and_preserves_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore.create(root/'runs', signal_catalog={}, requested_period_ns=20_000_000)
            rows = waveform()
            emitter = ActionEventEmitter(store.append_action_event,
                monotonic_clock_ns=lambda: rows[0][2], utc_clock_ns=lambda: rows[0][3])
            emitter.observe(ActionName.TIP_SHAPER_PARAMETERS, before={},
                after={'available': True, 'parameters': PATTERN['parameters'], 'scope': 'configuration_not_execution'},
                changed_fields=('parameters',), source='fake_readback')
            append_signals(store, rows)
            store.finalize('complete', stopped_utc_ns=time.time_ns())
            store.close()
            before = hashlib.sha256(store.database_path.read_bytes()).hexdigest()
            review = publish_recording_review(store.database_path, root/'review')
            self.assertEqual(len(review['tip_shaper_recognition']['detections']), 1)
            self.assertEqual(review['tip_shaper_recognition']['detections'][0]['parameter_source'], 'recorded_configuration')
            self.assertEqual([e['kind'] for e in review['timeline']], ['tip_shaper_inferred'])
            report = Path(review['report_path']).read_text(encoding='utf-8')
            self.assertIn('扎针／Tip Shaper（自动识别）', report)
            self.assertIn('signal seq', report)
            self.assertEqual(hashlib.sha256(store.database_path.read_bytes()).hexdigest(), before)

    def test_signal_reader_obeys_watermark_with_newer_rows_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore.create(tmp, signal_catalog={}, requested_period_ns=20_000_000)
            append_signals(store, waveform()[:10])
            store.finalize('complete', stopped_utc_ns=time.time_ns())
            store.close()
            self.assertEqual(len(list(iter_recording_signals(store.database_path, maximum_seq=3, chunk_size=2))), 4)
            self.assertEqual(list(iter_recording_signals(store.database_path, maximum_seq=-1)), [])


class TipShaperVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads((Path(__file__).parent / 'fixtures/tip_shaper_variants.json').read_text(encoding='utf-8'))['cases']

    def test_anonymized_recorded_variants_and_competing_processes(self):
        for case in self.cases:
            with self.subTest(case=case['name']):
                result = recognize_tip_shaper(case['signals'], PATTERN, observation_context=case['context'])
                self.assertTrue(result['complete'])
                self.assertEqual(len(result['detections']), len(case['expected']))
                for actual, expected in zip(result['detections'], case['expected']):
                    self.assertLess(abs(actual['start_monotonic_ns']-expected['observed_feedback_off_ns']), .3e9)
                    self.assertAlmostEqual(-actual['observed_z_dip_m'], expected['approach_m'], delta=.035e-9)
                    self.assertAlmostEqual(actual['observed_z_return_m'], expected['return_m'], delta=.035e-9)
                    self.assertEqual(actual['post_return']['extra_retraction_m']>2e-9, expected['extra_retraction_above_2nm'])
                    self.assertEqual(actual['parameter_source'], 'waveform_inferred')
                    self.assertNotIn('bias_v', actual['matched_parameters'])
                    self.assertIsNotNone(actual['feedback_interval'])
                    self.assertEqual(actual['outcome'], 'not_assessed')

    def test_amplitude_changes_without_parameter_snapshots(self):
        for down, up in ((.7,.6),(.8,.8),(1.,.8),(.6,.8),(2.,3.)):
            with self.subTest(down=down, up=up):
                rows = waveform(PATTERN['parameters'] | {'tip_lift_m':-down*1e-9,'lift_height_m':up*1e-9})
                events = recognize_tip_shaper(rows, PATTERN)['detections']
                self.assertEqual(len(events), 1)
                self.assertAlmostEqual(events[0]['observed_z_return_m']*1e9, up, delta=.03)
                self.assertEqual(events[0]['parameter_source'], 'waveform_inferred')

    def test_feedback_seed_handles_near_zero_baseline_without_current_trigger(self):
        rows = waveform(PATTERN['parameters'] | {'tip_lift_m':-.8e-9,'lift_height_m':.6e-9})
        for row in rows:
            if row[2] < 101_080_000_000: row[4] = 0.
        context = {'feedback_intervals':[{'start_monotonic_ns':101_000_000_000,
                   'end_monotonic_ns':101_850_000_000,'start_action_event_id':1,'end_action_event_id':2}]}
        events = recognize_tip_shaper(rows,PATTERN,observation_context=context)['detections']
        self.assertEqual(len(events),1)
        self.assertTrue(events[0]['current_ratio_uses_floor'])
        self.assertEqual(events[0]['baseline_current_a'],0.)
        self.assertFalse(events[0]['parameters_are_execution_proof'])

    def test_closed_feedback_is_not_sufficient_and_running_processes_are_excluded(self):
        rows = waveform()
        for kind in ('auto_approach','bias_spectroscopy'):
            context = {'excluded_process_intervals':[{'kind':kind,'start_monotonic_ns':100_000_000_000,'end_monotonic_ns':None}]}
            self.assertEqual(recognize_tip_shaper(rows,PATTERN,observation_context=context)['detections'],[])
        context = {'feedback_intervals':[{'start_monotonic_ns':101_000_000_000,
                   'end_monotonic_ns':101_850_000_000,'start_action_event_id':1,'end_action_event_id':2}]}
        for row in rows: row[7] = 10e-9
        self.assertEqual(recognize_tip_shaper(rows,PATTERN,observation_context=context)['detections'],[])

    def test_xy_motion_outside_the_core_does_not_discard_stationary_operation(self):
        rows = waveform(PATTERN['parameters'] | {'tip_lift_m':-.8e-9,'lift_height_m':.8e-9})
        for row in rows:
            phase=(row[2]-101_000_000_000)/1e9
            row[5] = min(phase,0)*1e-8 + max(phase-.85,0)*1e-8
        self.assertEqual(len(recognize_tip_shaper(rows,PATTERN)['detections']),1)

    def test_completed_event_at_boundary_survives_but_post_return_is_incomplete(self):
        case = self.cases[0]
        first = recognize_tip_shaper(case['signals'],PATTERN,observation_context=case['context'])['detections'][0]
        rows = [r for r in case['signals'] if r[2]<first['end_monotonic_ns']+.14e9]
        events = recognize_tip_shaper(rows,PATTERN,observation_context=case['context'])['detections']
        self.assertEqual(len(events),1)
        self.assertFalse(events[0]['post_return']['complete'])

    def test_state_context_uses_only_observed_nontruncated_closed_intervals(self):
        def action(i,on,**changes):
            return {'action_event_id':i,'monotonic_ns':i*1_000_000_000,'phase':'observed',
                    'action_name':'z_controller_state_change','arguments':{'value':{'after':{'z_controller_on':on}}}} | changes
        actions=[action(1,False),action(2,True,phase='before'),
                 action(3,True,arguments={'truncated':True,'value':{'after':{'z_controller_on':True}}}),
                 action(4,True),action(5,False)]
        context=tip_shaper_observation_context(actions,[])
        self.assertEqual(len(context['feedback_intervals']),1)
        self.assertEqual(context['feedback_intervals'][0]['end_action_event_id'],4)


class TipShaperParameterObserverTests(unittest.TestCase):
    def observer(self):
        events = []
        backend = FakeNanonisBackend()
        observer = ExternalStateObserver(NanonisDevice(backend=backend), ActionEventEmitter(events.append),
                                         observe_tip_shaper_parameters=True)
        return observer, backend, events

    def test_only_reads_on_audit_start_and_every_five_seconds(self):
        observer, backend, events = self.observer()
        with patch.object(backend, 'TipShaper_PropsGet', wraps=backend.TipShaper_PropsGet) as getter:
            with patch('workflow_layer.action_audit.time.perf_counter', return_value=10):
                observer._observe_tip_shaper_parameters()
                self.assertEqual(getter.call_count, 0)
                observer._observe_tip_shaper_parameters(force=True)
                observer._observe_tip_shaper_parameters()
            with patch('workflow_layer.action_audit.time.perf_counter', return_value=16):
                observer._observe_tip_shaper_parameters()
            self.assertEqual(getter.call_count, 2)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.action_name == ActionName.TIP_SHAPER_PARAMETERS for e in events))
        self.assertTrue(all(name == 'TipShaper_PropsGet' for name, _ in backend.command_log))
        self.assertEqual(events[0].arguments.value['after']['scope'], 'configuration_not_execution')

    def test_unsupported_query_is_recorded_once_without_retry_flood(self):
        observer, backend, events = self.observer()
        with patch.object(backend, 'TipShaper_PropsGet', side_effect=NanonisCommandError('unsupported')) as getter:
            observer._observe_tip_shaper_parameters(force=True)
            observer._observe_tip_shaper_parameters()
            observer._observe_tip_shaper_parameters(force=True)
            self.assertEqual(getter.call_count, 1)
        self.assertFalse(events[0].arguments.value['after']['available'])
        self.assertEqual(events[0].arguments.value['after']['error'], 'unsupported')

    def test_transport_failure_is_not_misreported_as_unsupported(self):
        observer, backend, _ = self.observer()
        with patch.object(backend, 'TipShaper_PropsGet', side_effect=TimeoutError('transport')):
            with self.assertRaises(TimeoutError):
                observer._observe_tip_shaper_parameters(force=True)

    def test_always_gate_captures_parameters_and_automatically_publishes_review(self):
        names = ('Current (A)', 'X (m)', 'Y (m)', 'Z (m)')
        telemetry = FakeNanonisBackend(signal_names=list(names), signal_values={i: 0. for i in range(4)})
        status = FakeNanonisBackend()
        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(NanonisDevice(backend=telemetry), tmp, signal_names=names,
                external_state_device=NanonisDevice(backend=status), observe_tip_shaper_parameters=True,
                storage_quota_bytes=10_000_000, storage_warning_bytes=9_000_000).record(duration_s=.12)
            self.assertIsNone(result.review_error)
            review = json.loads(Path(result.review_path).read_text(encoding='utf-8'))
            self.assertIn('tip_shaper_recognition', review)
            self.assertIn('TipShaper_PropsGet', [name for name, _ in status.command_log])
            self.assertNotIn('TipShaper_Start', [name for name, _ in status.command_log])


if __name__ == '__main__':
    unittest.main()
