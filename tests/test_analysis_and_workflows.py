from __future__ import annotations

from pathlib import Path
import unittest
from tempfile import TemporaryDirectory

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis_layer import (  # noqa: E402
    DeterministicTipQualityAnalyzer,
    LocalMaximumAtomDetector,
    ReferenceSpectrumClassifier,
    TipQualityAssessment,
    TipQualityDecision,
    TipQualityThresholds,
    pixel_center_to_xy,
)
from data_layer import TipConditioningRunState, RunStore  # noqa: E402
from knowledge_base.spectrum_reference import SpectrumReference
from instrument_layer import (  # noqa: E402
    BiasPulseRecipe,
    BiasSpectroscopyData,
    BiasSpectroscopyRecipe,
    BiasSpectroscopyTiming,
    FakeNanonisBackend,
    FeedbackPokeRecipe,
    NanonisDevice,
    ScanFrame,
    ScanFrameData,
)
from workflow_layer import (  # noqa: E402
    AtomSpectroscopyWorkflow,
    AutomaticTipConditioningWorkflow,
    TipConditioningCommand,
)


def scan_with_two_peaks() -> list[list[float]]:
    data = [[0.0 for _column in range(7)] for _row in range(7)]
    data[2][2] = 10.0
    data[4][4] = 8.0
    return data


def spectroscopy_recipe() -> BiasSpectroscopyRecipe:
    return BiasSpectroscopyRecipe(
        -0.1,
        0.1,
        1,
        5,
        (0, 1),
        BiasSpectroscopyTiming(0.01, 0.0, 0.01, 10.0, 0.001, 0.002, 0.01, 0.01),
    )


class AnalysisAndWorkflowTests(unittest.TestCase):
    def test_local_maximum_detector_returns_physical_coordinates(self) -> None:
        frame = ScanFrame(1e-9, 2e-9, 7e-9, 7e-9, 0.0)

        candidates = LocalMaximumAtomDetector().detect(scan_with_two_peaks(), frame)

        self.assertEqual(len(candidates), 2)
        self.assertEqual((candidates[0].row, candidates[0].column), (2, 2))
        self.assertAlmostEqual(candidates[0].x_m, 0.0)
        self.assertAlmostEqual(candidates[0].y_m, 3e-9)

    def test_positive_scan_angle_uses_nanonis_clockwise_convention(self) -> None:
        frame = ScanFrame(0.0, 0.0, 2.0, 2.0, 90.0)

        x_m, y_m = pixel_center_to_xy(frame, 1, 2, 0, 1)

        self.assertAlmostEqual(x_m, 0.0, places=12)
        self.assertAlmostEqual(y_m, -0.5, places=12)

    def test_spectrum_classifier_rejects_out_of_distribution_spectrum(self) -> None:
        classifier = ReferenceSpectrumClassifier(
            [SpectrumReference("synthetic-a", "species-a", "Current (A)", (-0.1, 0.0, 0.1),
                               (1.0, 0.0, 0.0), (), "synthetic", "test", "test fixture")],
            channel_name="Current (A)",
            maximum_distance=0.1,
            allow_synthetic=True,
        )
        spectrum = BiasSpectroscopyData(
            ("Current (A)", "Bias calc (V)"),
            2,
            3,
            ((0.0, 1.0, 0.0), (-0.1, 0.0, 0.1)),
            (),
        )

        result = classifier.classify(spectrum)

        self.assertEqual(result.label, "unknown")
        self.assertEqual(result.reason, "outside_reference_distance")

    def test_deterministic_tip_analyzer_exposes_metrics(self) -> None:
        analyzer = DeterministicTipQualityAnalyzer(
            TipQualityThresholds(minimum_contrast=5.0, maximum_line_jump_ratio=2.0)
        )
        scan = ScanFrameData(
            0,
            "Z (m)",
            "forward",
            5,
            5,
            [[0.0, 0.0, 10.0, 0.0, 0.0] for _row in range(5)],
            1,
        )

        assessment = analyzer.assess(scan)

        self.assertEqual(assessment.decision, TipQualityDecision.GOOD)
        self.assertIn("line_jump_ratio", assessment.metrics)

    def test_atom_workflow_detects_moves_spectra_and_classifies(self) -> None:
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        bias_values = [-0.1, -0.05, 0.0, 0.05, 0.1]
        current_reference = [2.0 * (value * value + 1e-6) for value in bias_values]
        classifier = ReferenceSpectrumClassifier(
            [SpectrumReference("synthetic-a", "species-a", "Current (A)", tuple(bias_values),
                               tuple(current_reference), (), "synthetic", "test", "FakeNanonisBackend")],
            channel_name="Current (A)",
            maximum_distance=0.1,
            axis_channel_name="Bias (V)",
            allow_synthetic=True,
        )
        workflow = AtomSpectroscopyWorkflow(
            device,
            LocalMaximumAtomDetector(),
            classifier,
            spectroscopy_recipe(),
        )
        scan = ScanFrameData(0, "Z (m)", "forward", 7, 7, scan_with_two_peaks(), 1)

        with TemporaryDirectory() as directory:
            store = RunStore.create(directory, signal_catalog={}, requested_period_ns=0)
            try:
                atom_map = workflow.run(scan, frame=device.get_scan_frame(), run_store=store)
            finally:
                store.close()

        self.assertEqual(len(atom_map.atoms), 2)
        self.assertTrue(all(atom.classification == "species-a" for atom in atom_map.atoms))
        self.assertEqual(atom_map.coordinate_transform.version, "nanonis-scan-frame-v1")
        self.assertTrue(all(atom.move.position_after == atom.move.target for atom in atom_map.atoms))
        self.assertEqual(
            [name for name, _args in backend.command_log].count("BiasSpectr_Start"),
            2,
        )

    def test_tip_conditioning_requires_precheck_then_postcheck(self) -> None:
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        workflow = AutomaticTipConditioningWorkflow(device, minimum_precheck_confidence=0.8)
        x_m, y_m = backend.scan_frame[:2]
        command = TipConditioningCommand(x_m, y_m, BiasPulseRecipe(0.5, 10.0))
        precheck = TipQualityAssessment(TipQualityDecision.DEGRADED, 0.2, 0.95, "double_tip")

        pending = workflow.execute_action(command, precheck)

        self.assertEqual(pending.state, TipConditioningRunState.AWAITING_POSTCHECK)
        self.assertIsNotNone(pending.move_result)
        self.assertIsNotNone(pending.action_result)
        self.assertIn("Bias_Pulse", [name for name, _args in backend.command_log])

        completed = workflow.complete_postcheck(
            pending,
            TipQualityAssessment(TipQualityDecision.GOOD, 0.9, 0.9, "single_apex"),
            minimum_score_improvement=0.2,
        )
        self.assertEqual(completed.state, TipConditioningRunState.SUCCEEDED)

    def test_tip_conditioning_workflow_accepts_feedback_managed_poke(self) -> None:
        backend = FakeNanonisBackend(scan_running=False, bias_v=-0.074)
        workflow = AutomaticTipConditioningWorkflow(
            NanonisDevice(backend=backend),
            minimum_precheck_confidence=0.8,
        )
        x_m, y_m = backend.scan_frame[:2]

        record = workflow.execute_action(
            TipConditioningCommand(
                x_m,
                y_m,
                FeedbackPokeRecipe(approach_depth_m=1e-9),
            ),
            TipQualityAssessment(
                TipQualityDecision.DEGRADED,
                0.2,
                0.95,
                "poke_candidate",
            ),
        )

        self.assertEqual(record.state, TipConditioningRunState.AWAITING_POSTCHECK)
        self.assertEqual(record.action_result.action, "feedback_poke")
        self.assertIn("TipShaper_Start", [name for name, _args in backend.command_log])

    def test_tip_conditioning_does_not_act_on_uncertain_precheck(self) -> None:
        backend = FakeNanonisBackend(scan_running=False)
        workflow = AutomaticTipConditioningWorkflow(
            NanonisDevice(backend=backend), minimum_precheck_confidence=0.8
        )
        x_m, y_m = backend.scan_frame[:2]
        command = TipConditioningCommand(x_m, y_m, BiasPulseRecipe(0.5, 10.0))

        record = workflow.execute_action(
            command,
            TipQualityAssessment(TipQualityDecision.UNCERTAIN, 0.4, 0.5, "ambiguous"),
        )

        self.assertEqual(record.state, TipConditioningRunState.PRECHECK_REJECTED)
        self.assertNotIn("Bias_Pulse", [name for name, _args in backend.command_log])

    def test_tip_conditioning_run_once_forces_fresh_pre_and_post_scans(self) -> None:
        class ScriptedAnalyzer:
            def __init__(self) -> None:
                self.items = [
                    TipQualityAssessment(TipQualityDecision.DEGRADED, 0.2, 0.95, "double_tip"),
                    TipQualityAssessment(TipQualityDecision.GOOD, 0.9, 0.95, "single_apex"),
                ]

            def assess(self, _scan):
                return self.items.pop(0)

        backend = FakeNanonisBackend(scan_running=False)
        workflow = AutomaticTipConditioningWorkflow(
            NanonisDevice(backend=backend), minimum_precheck_confidence=0.8
        )
        x_m, y_m = backend.scan_frame[:2]
        command = TipConditioningCommand(x_m, y_m, BiasPulseRecipe(0.5, 10.0))

        cycle = workflow.run_once(
            command,
            ScriptedAnalyzer(),
            channel_index=0,
            minimum_score_improvement=0.2,
        )

        self.assertEqual(cycle.run_record.state, TipConditioningRunState.SUCCEEDED)
        self.assertIsNotNone(cycle.postcheck_scan)
        names = [name for name, _args in backend.command_log]
        self.assertEqual(names.count("Scan_WaitEndOfScan"), 2)
        self.assertEqual(names.count("Bias_Pulse"), 1)


if __name__ == "__main__":
    unittest.main()
