"""Offline quality decisions must reject deceptively quiet or incomplete evidence."""
import hashlib
import gzip
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_layer.signal_windows import analyze_signal_window, compare_pulse_windows, STABILITY_LIMIT_METRICS
from data_layer import RunStore, TelemetrySample
from runtime_layer.spm_tools import SPMTools


def samples():
    rng = np.random.default_rng(40)
    n = 201
    return np.column_stack((np.arange(n), np.zeros(n), np.arange(n) * 20_000_000,
                            np.arange(n) * 20_000_000 + 1_700_000_000_000_000_000,
                            100e-12 + rng.normal(0, .1e-12, n), np.zeros(n), np.zeros(n),
                            rng.normal(0, .2e-12, n)))


def options():
    # Synthetic test thresholds, not station defaults or approved hardware limits.
    return dict(minimum_duration_s=3, maximum_sample_gap_s=.03, expected_current_a=100e-12,
                current_full_scale_a=10e-9, z_limits_m=[-1e-6, 1e-6],
                analysis_limits=dict(zip(STABILITY_LIMIT_METRICS,
                    (1e-12, 5e-12, 1e-12, .1, 1e-12, 5e-12, 1e-12, 1e-12, .05))))


class SignalWindowTests(unittest.TestCase):
    def test_anonymized_recorded_pulses_keep_rail_noise_and_z_independent(self):
        path=Path(__file__).parent/'fixtures/pulse_response_traces.json.gz'
        with gzip.open(path,'rt',encoding='utf-8') as source:
            cases=json.load(source)['cases']
        reports=[compare_pulse_windows(c['before'],c['during'],c['after'],
            minimum_duration_s=.1,maximum_sample_gap_s=.1,expected_current_a=100e-12,
            current_full_scale_a=10e-9) for c in cases]
        self.assertEqual(len(reports),8)
        self.assertTrue(all(r['comparison_valid'] for r in reports))
        rail,quieter=reports[2],reports[6]
        self.assertTrue(rail['near_full_scale'])
        self.assertGreater(rail['current_rms_ratio'],40)
        self.assertAlmostEqual(rail['delta_z_m']*1e9,11.0914,places=3)
        self.assertFalse(quieter['near_full_scale'])
        self.assertAlmostEqual(quieter['peak_abs_current_a']*1e9,4.62415,places=4)
        self.assertAlmostEqual(quieter['current_rms_before_a']*1e12,123.416,places=2)
        self.assertAlmostEqual(quieter['current_rms_after_a']*1e12,24.3311,places=3)
        self.assertAlmostEqual(quieter['delta_z_m']*1e9,-8.43936,places=4)
        self.assertTrue(all(not r['peak_complete'] for r in reports))

    def test_pulse_response_keeps_three_facts_separate_and_missing_peak_unknown(self):
        before=samples();after=before.copy();after[:,4]=100e-12+(after[:,4]-100e-12)*2
        after[:,7]+=3e-9
        kw=dict(minimum_duration_s=3,maximum_sample_gap_s=.03,expected_current_a=100e-12,
                current_full_scale_a=10e-9)
        q=compare_pulse_windows(before,[],after,**kw)
        self.assertIsNone(q['near_full_scale'])
        self.assertIsNone(q['peak_abs_current_a'])
        self.assertAlmostEqual(q['current_rms_ratio'],2)
        self.assertAlmostEqual(q['delta_z_m']*1e9,3)
        self.assertEqual(q['z_direction'],'increased')
        moved=after.copy(); moved[:,5]=1e-9
        q=compare_pulse_windows(before,[],moved,**kw)
        self.assertFalse(q['comparison_valid'])
        self.assertFalse(q['same_xy'])
        after[70,0]+=1
        q=compare_pulse_windows(before,[],after,**kw)
        self.assertFalse(q['comparison_valid'])
        self.assertIsNone(q['delta_z_m'])

    def test_stable_candidate_and_uncalibrated_metrics_are_distinct(self):
        report = analyze_signal_window(samples(), **options())
        self.assertEqual(report['status'], 'stable_candidate')
        self.assertFalse(report['authorizes_live_action'])
        metrics = analyze_signal_window(samples(), minimum_duration_s=3, maximum_sample_gap_s=.03)
        self.assertEqual(metrics['status'], 'metrics_only')
        self.assertIn('analysis_limits', metrics['missing_assessment_inputs'])
        json.dumps(report, allow_nan=False)

    def test_bursts_hidden_by_median_and_small_adjacent_steps_fail_rms(self):
        data = samples()
        # A smooth transient with small per-sample changes and an unchanged median.
        data[70:130, 4] += 40e-12 * np.sin(np.linspace(0, np.pi, 60)) ** 2
        result = analyze_signal_window(data, **options())
        self.assertEqual(result['status'], 'unstable')
        self.assertEqual(result['threshold_failures'], ['current_rms_fraction'])
        self.assertGreater(result['metrics']['current_abs_p95_a'], 130e-12)

    def test_recorded_burst_window_rejects_only_on_added_rms(self):
        import gzip
        with gzip.open(Path(__file__).parent / 'fixtures/noise_transient.json.gz', 'rt', encoding='utf-8') as f:
            recorded = json.load(f)
        result = analyze_signal_window(recorded['rows'], minimum_duration_s=1.8,
            maximum_sample_gap_s=.5, expected_current_a=100e-12, current_full_scale_a=1e-8,
            z_limits_m=[-98.9e-9,98.9e-9], analysis_limits=recorded['analysis_limits'])
        self.assertEqual(result['status'], 'unstable')
        self.assertEqual(result['threshold_failures'], ['current_rms_fraction'])

    def test_negative_bias_current_uses_setpoint_magnitude(self):
        data = samples()
        data[:, 4] *= -1
        result = analyze_signal_window(data, **options())
        self.assertEqual(result['status'], 'stable_candidate')
        self.assertLess(result['metrics']['current_median_a'], 0)
        self.assertLess(result['metrics']['current_setpoint_error_fraction'], .01)

    def test_old_limits_cannot_silently_skip_whole_window_noise(self):
        old = options()
        del old['analysis_limits']['current_rms_fraction']
        with self.assertRaises(ValueError):
            analyze_signal_window(samples(), **old)

    def test_low_noise_does_not_hide_withdrawal_drift_jumps_or_motion(self):
        for kind in ('withdrawal', 'drift', 'jump', 'motion', 'z_jump'):
            with self.subTest(kind=kind):
                data = samples()
                if kind == 'withdrawal': data[:, 4] -= 100e-12
                if kind == 'drift': data[:, 7] += np.arange(len(data)) * 1e-12
                if kind == 'jump': data[100:, 4] += 40e-12
                if kind == 'motion': data[:, 5] = np.arange(len(data)) * 1e-12
                if kind == 'z_jump': data[100:, 7] += 1e-9
                report = analyze_signal_window(data, **options())
                self.assertEqual(report['status'], 'unstable')
                self.assertTrue(report['threshold_failures'])

    def test_gaps_segments_freeze_clipping_and_nonfinite_are_invalid(self):
        for kind in ('gap', 'segment', 'clock', 'missing_seq', 'freeze', 'clip', 'z_limit', 'nan', 'short'):
            with self.subTest(kind=kind):
                data = samples()
                if kind == 'gap': data[100:, 2] += 100_000_000
                if kind == 'segment': data[100:, 1] = 1
                if kind == 'clock': data[100, 2] = data[99, 2]
                if kind == 'missing_seq': data[100:, 0] += 1
                if kind == 'freeze': data[:, 4], data[:, 7] = 100e-12, 0
                if kind == 'clip': data[:, 4] = 10e-9
                if kind == 'z_limit': data[:, 7] = 1e-6
                if kind == 'nan': data[100, 4] = np.nan
                if kind == 'short': data = data[:80]
                report = analyze_signal_window(data, **options())
                self.assertEqual(report['status'], 'invalid_data')
                self.assertTrue(report['quality_flags'])
                json.dumps(report, allow_nan=False)

    def test_partial_thresholds_and_invalid_context_are_rejected(self):
        for changes in ({'analysis_limits': {'current_difference_noise_a': 1e-12}},
                        {'expected_current_a': 0}, {'current_full_scale_a': float('inf')},
                        {'maximum_sample_gap_s': -1}, {'z_limits_m': [1, -1]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                analyze_signal_window(samples(), **(options() | changes))

    def test_existing_reader_tool_source_scope_and_partial_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RunStore.create(root / 'runs', signal_catalog={}, requested_period_ns=20_000_000)
            for row in samples():
                seq, segment, mono, utc, current, x, y, z = row
                store.append_sample(TelemetrySample(int(seq), int(segment), int(mono), int(utc),
                    int(mono), int(mono), current, x, y, z))
            store.finalize('complete', stopped_utc_ns=time.time_ns())
            store.close()
            before = hashlib.sha256(store.database_path.read_bytes()).hexdigest()
            tools = SPMTools(read_roots=[root / 'runs'], output_root=root / 'out')
            result = tools.inspect_signal_window(str(store.database_path), 10, 200, **options()).data
            self.assertEqual(result['status'], 'stable_candidate')
            self.assertEqual(result['sample_count'], 191)
            self.assertEqual(result['first_seq'], 10)
            self.assertTrue(Path(result['report_path']).is_file())
            again = tools.inspect_signal_window(str(store.database_path), 10, 200, **options()).data
            self.assertEqual(result['source_rows_sha256'], again['source_rows_sha256'])
            partial = tools.inspect_signal_window(str(store.database_path), 10, 201, **options()).data
            self.assertEqual(partial['status'], 'invalid_data')
            self.assertIn('requested_window_not_fully_committed', partial['quality_flags'])
            self.assertEqual(before, hashlib.sha256(store.database_path.read_bytes()).hexdigest())
            with self.assertRaises(ValueError):
                tools.inspect_signal_window(str(store.database_path), 0, 100000, **options())
            outsider = root / 'outside.sqlite'
            outsider.write_bytes(b'not a database')
            with self.assertRaises(ValueError):
                tools.inspect_signal_window(str(outsider), 0, 2, **options())


if __name__ == '__main__':
    unittest.main()
