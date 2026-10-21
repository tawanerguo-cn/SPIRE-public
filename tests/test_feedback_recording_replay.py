import gzip
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from replay_feedback_recording import replay_window


class RealFeedbackReplayTests(unittest.TestCase):
    def test_anonymized_recorded_transients_do_not_force_withdrawal(self):
        fixture = Path(__file__).parent / "fixtures/feedback_recovery_traces.json.gz"
        with gzip.open(fixture, "rt", encoding="utf-8") as stream:
            windows = json.load(stream)["windows"]
        old_withdrawals = 0
        for window in windows:
            metadata, rows = window["metadata"], window["rows"]
            with self.subTest(sequence=metadata["sequence"]):
                digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
                self.assertEqual(digest, metadata["source_rows_sha256"])
                old = replay_window(rows)
                old_withdrawals += old.get("protective_withdraw", False)
                actual = replay_window(rows, settling_s=3., overcurrent_hold_s=.5,
                                       withdraw_on_overcurrent=0)
                self.assertFalse(actual.get("protective_withdraw", False))
                if metadata["expected_status"] == "insufficient_evidence":
                    self.assertEqual(actual["status"], "insufficient_evidence")
                elif metadata["expected_status"] == "unsettled":
                    self.assertEqual(actual["status"], "unsettled")
                else:
                    self.assertEqual(actual["status"], "completed")
        self.assertGreaterEqual(old_withdrawals, 5)

    def test_anonymized_recorded_persistent_excursion_is_flagged_without_forced_withdrawal(self):
        fixture = Path(__file__).parent / "fixtures/feedback_excursion_traces.json.gz"
        with gzip.open(fixture, "rt", encoding="utf-8") as stream:
            windows = json.load(stream)["windows"]
        statuses = []
        for window in windows:
            rows = window["rows"]
            self.assertEqual(hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
                             window["metadata"]["source_rows_sha256"])
            result = replay_window(rows, settling_s=3., overcurrent_hold_s=.5,
                                   withdraw_on_overcurrent=0)
            statuses.append(result["status"])
            self.assertFalse(result.get("protective_withdraw", False))
            if result["status"] == "not_recovered":
                self.assertAlmostEqual(result["elapsed_s"], 13.0602529, places=5)
        self.assertEqual(statuses, ["completed", "not_recovered", "insufficient_evidence"])

    def test_a_segment_boundary_is_not_filled_or_treated_as_recovery(self):
        rows = [[0,0,0,0,1e-10,0,0,0], [1,1,20000000,20000000,1e-10,0,0,0]]
        self.assertEqual(replay_window(rows)["status"], "insufficient_evidence")

    def test_anonymized_recorded_bad_spectrum_still_protects_and_stable_control_does_not(self):
        fixture = Path(__file__).parent / 'fixtures/spectroscopy_guard_traces.json.gz'
        with gzip.open(fixture, 'rt', encoding='utf-8') as stream:
            windows = json.load(stream)['windows']
        for window in windows:
            with self.subTest(label=window['label']):
                rows = window['rows']
                self.assertEqual(hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest(),
                                 window['source_rows_sha256'])
                result = replay_window(rows, maximum_abs_current_a=2e-9, overcurrent_hold_s=.1,
                                       observation_mode='spectroscopy', maximum_sample_gap_s=.5)
                if window['label'] == 'clipped_spectrum':
                    self.assertEqual(result['status'], 'aborted')
                    self.assertTrue(result['protective_withdraw'])
                    self.assertAlmostEqual(result['elapsed_s'], 6.182, places=2)
                else:
                    self.assertEqual(result['status'], 'completed')
                    self.assertFalse(result['protective_withdraw'])


if __name__ == "__main__": unittest.main()
