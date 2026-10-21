from __future__ import annotations

from dataclasses import replace
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_layer import AtomDetectorSettings, LocalMaximumAtomDetector, ReferenceSpectrumClassifier
from data_layer import RunStore
from data_layer.atom_plan import read_atom_plan, write_json_artifact
from data_layer.recording import read_run_event_page
from instrument_layer import BiasSpectroscopyData, FakeNanonisBackend, NanonisDevice, ScanFrame, ScanFrameData
from knowledge_base.spectrum_reference import SpectrumReference, load_spectrum_library
from runtime_layer.spm_tools import SPMTools
from workflow_layer.atom_campaign import rehearsal_recipe, rehearse_atom_plan
from workflow_layer.atom_spectroscopy import AtomSpectroscopyWorkflow, AtomSpectroscopyWorkflowConfig
from workflow_layer.atom_targeting import prepare_atom_plan, validate_atom_plan
from workflow_layer.scan_observation import observe_saved_scan
from workflow_layer.spectrum_observation import inspect_saved_spectrum
from test_tip_reference_verification import _write_sxm
from test_analysis_and_workflows import scan_with_two_peaks, spectroscopy_recipe


def reference(label="test-species", **changes):
    defaults = dict(reference_id=label, label=label, channel_name="Current (A)", bias_v=(-.1, 0., .1),
                    values=(1., .2, .5), conditions=(("sample", "test sample"),), status="reviewed",
                    reviewed_by="test fixture author", source="synthetic test fixture, not lab knowledge")
    return SpectrumReference(**(defaults | changes))


def spectrum(values=(1., .2, .5), axis=(-.1, 0., .1)):
    return BiasSpectroscopyData(("Bias calc (V)", "Current (A)"), 2, len(axis), (axis, values), ())


class SpectralEvidenceTests(unittest.TestCase):
    def classifier(self, references=None, **kwargs):
        return ReferenceSpectrumClassifier(references if references is not None else [reference()],
               channel_name="Current (A)", maximum_distance=.1, conditions={"sample": "test sample"}, **kwargs)

    def test_descending_axis_and_signal_are_reversed_together(self):
        result = self.classifier().classify(spectrum((.5, .2, 1.), (.1, 0., -.1)))
        self.assertEqual(result.label, "test-species")
        self.assertEqual(result.reference_id, "test-species")
        self.assertIn("not_calibrated", result.score_kind)

    def test_equal_length_wrong_axis_is_rejected(self):
        result = self.classifier().classify(spectrum(axis=(-.01, 0., .01)))
        self.assertEqual(result.reason, "bias_axis_mismatch")

    def test_axis_nonfinite_constant_and_metadata_fail_closed(self):
        cases = [(spectrum(axis=(-.1, 0., 0.)), "bias_axis_not_monotonic"),
                 (spectrum(values=(0., 0., 0.)), "constant_or_empty_signal"),
                 (spectrum(values=(1., float("nan"), 2.)), "nonfinite_data"),
                 (replace(spectrum(), rows=3), "malformed_spectrum"),
                 (replace(spectrum(), channel_names=("Other (V)", "Current (A)")), "bias_axis_not_recorded")]
        for data, reason in cases:
            with self.subTest(reason=reason):
                result = self.classifier().classify(data)
                self.assertEqual((result.label, result.reason), ("unknown", reason))
                json.dumps(result.__dict__, allow_nan=False)

    def test_ambiguous_labels_cannot_win_alphabetically(self):
        result = self.classifier([reference("A"), reference("B")]).classify(spectrum())
        self.assertEqual(result.reason, "ambiguous_reference_match")

    def test_missing_conditions_and_synthetic_references_cannot_label_lab_data(self):
        classifier = ReferenceSpectrumClassifier([reference()], channel_name="Current (A)", maximum_distance=.1)
        self.assertEqual(classifier.classify(spectrum()).reason, "no_applicable_reviewed_reference")
        synthetic = reference(status="synthetic")
        self.assertEqual(self.classifier([synthetic]).classify(spectrum()).label, "unknown")
        self.assertEqual(self.classifier([synthetic], allow_synthetic=True).classify(spectrum()).label, "test-species")

    def test_reference_schema_requires_review_and_source(self):
        for changes in ({"status": "draft"}, {"reviewed_by": ""}, {"conditions": ()},
                        {"bias_v": (0., 0., .1)}, {"values": (0., 0., 0.)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reference(**changes)
        with self.assertRaises(TypeError):
            self.classifier({"legacy": [1, 2, 3]})

    def test_saved_spectrum_zero_sweeps_are_flagged_and_not_substituted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spectrum.dat"
            path.write_text("X (m)\t1e-9\n[DATA]\nBias calc (V)\tLI Demod 1 X [AVG] (A)\tLI Demod 1 X [00002] (A)\n-.1\t1e-12\t0\n0\tNaN\t0\n.1\t2e-12\t0\n")
            result, _ = inspect_saved_spectrum(path, channel="LI Demod 1 X [AVG] (A)")
            self.assertEqual(result["classification"]["reason"], "nonfinite_data")
            self.assertEqual(result["quality"]["LI Demod 1 X [AVG] (A)"]["finite_count"], 2)
            self.assertTrue(result["quality"]["LI Demod 1 X [00002] (A)"]["all_zero"])


class AtomPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "saved.sxm"
        _write_sxm(self.path)
        self.path.write_bytes(self.path.read_bytes().replace(b":SCAN_DIR:", b":SCAN_ANGLE:\n30\n:SCAN_DIR:"))
        self.observation = observe_saved_scan(self.path)
        self.selections = [{"row": 15, "column": 20, "selected_by": "test visual annotation",
                            "rationale": "test feature; identity unverified"}]
        self.plan = prepare_atom_plan(self.observation, self.selections)

    def test_plan_roundtrip_is_source_and_coordinate_bound(self):
        path = write_json_artifact(self.root / "plan.json", self.plan.to_dict())
        loaded = read_atom_plan(path)
        self.assertEqual(loaded, self.plan)
        self.assertEqual(validate_atom_plan(loaded).source_sha256, self.observation.source_sha256)
        target = replace(self.plan.targets[0], x_m=0.01)
        with self.assertRaisesRegex(ValueError, "geometry"):
            validate_atom_plan(replace(self.plan, targets=(target,)))

    def test_mutated_file_invalidates_old_plan(self):
        self.path.write_bytes(self.path.read_bytes().replace(b"30\n:SCAN_DIR:", b"31\n:SCAN_DIR:"))
        with self.assertRaisesRegex(ValueError, "changed"):
            validate_atom_plan(self.plan)

    def test_missing_pixels_exclusions_and_duplicates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "excluded"):
            prepare_atom_plan(self.observation, self.selections, excluded_regions=((14, 17, 19, 22),))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            prepare_atom_plan(self.observation, self.selections * 2)
        mask = self.observation.valid_mask.copy()
        mask[14, 20] = False
        with self.assertRaisesRegex(ValueError, "unacquired"):
            prepare_atom_plan(replace(self.observation, valid_mask=mask), self.selections)
        with self.assertRaises(ValueError):
            prepare_atom_plan(self.observation, [self.selections[0] | {"row": 15.5}])

    def test_fake_campaign_persists_raw_spectra_and_unknown_map(self):
        result = rehearse_atom_plan(self.plan, self.root / "runs")
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["atom_map"]["atoms"][0]["classification"], "unknown")
        with closing(sqlite3.connect(Path(result["run_directory"]) / "run.sqlite")) as connection:
            events = connection.execute("SELECT event_type FROM events ORDER BY event_id").fetchall()
            self.assertLess(events.index(("atom_spectrum_acquired",)), events.index(("atom_target_completed",)))
            self.assertGreater(connection.execute("SELECT count(*) FROM action_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM artifacts").fetchone()[0], 3)

    def test_fake_campaign_is_bounded_before_movement(self):
        result = rehearse_atom_plan(self.plan, self.root / "runs", maximum_atoms=21)
        self.assertEqual(result["status"], "failed")
        with closing(sqlite3.connect(Path(result["run_directory"]) / "run.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM action_events").fetchone()[0], 0)

    def test_runtime_file_roots_and_source_identity(self):
        root = self.root / "inputs"
        root.mkdir()
        tools = SPMTools(read_roots=[root], output_root=self.root / "derived")
        with self.assertRaisesRegex(ValueError, "outside"):
            tools.observe_scan(str(self.path), mode="metrics")
        tools = SPMTools(read_roots=[self.root], output_root=self.root / "derived")
        with self.assertRaisesRegex(ValueError, "changed"):
            tools.prepare_atom_plan(str(self.path), "scan-wrong", self.selections)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "optional validation dependency")
    def test_runtime_native_images_and_shared_observation_identity(self):
        tools = SPMTools(read_roots=[self.root], output_root=self.root / "derived")
        metric = tools.observe_scan(str(self.path), mode="metrics")
        visual = tools.observe_scan(str(self.path), mode="image")
        self.assertEqual(metric.data["observation_id"], visual.data["observation_id"])
        self.assertFalse(metric.images)
        self.assertTrue(visual.images[0].startswith(b"\x89PNG"))
        self.assertNotIn("candidates", visual.data)
        prepared = tools.prepare_atom_plan(str(self.path), visual.data["observation_id"], self.selections)
        result = tools.run_fake_atom_plan(prepared.data["plan_path"])
        self.assertEqual(result.data["status"], "complete")
        self.assertEqual(result.data["atoms"][0]["classification"], "unknown")


class CampaignFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = RunStore.create(temporary.name, signal_catalog={}, requested_period_ns=0)
        self.addCleanup(self.store.close)
        self.backend = FakeNanonisBackend(scan_running=False)
        self.device = NanonisDevice(backend=self.backend)
        self.frame = self.device.get_scan_frame()
        self.scan = ScanFrameData(0, "Z (m)", "forward", 7, 7, scan_with_two_peaks(), 1)

    def workflow(self, classifier=None, config=None):
        return AtomSpectroscopyWorkflow(self.device, LocalMaximumAtomDetector(), classifier or
               ReferenceSpectrumClassifier(channel_name="Current (A)", maximum_distance=.1,
                                           axis_channel_name="Bias (V)"), spectroscopy_recipe(), config)

    def events(self, event_type):
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM events WHERE event_type=? ORDER BY event_id", (event_type,))]

    def test_invalid_recipe_is_rejected_before_any_target_move(self):
        for recipe in (replace(spectroscopy_recipe(), start_bias_v=6.0),
                       replace(spectroscopy_recipe(), channel_indexes=(0, 1.5))):
            workflow = self.workflow()
            workflow.recipe = recipe
            with self.subTest(recipe=recipe), self.assertRaises(Exception):
                workflow.run(self.scan, frame=self.frame, run_store=self.store)
        self.assertNotIn("FolMe_XYPosSet", [name for name, _ in self.backend.command_log])

    def test_classifier_failure_keeps_acquired_spectrum_and_stops(self):
        class BrokenClassifier:
            def classify(self, _spectrum):
                raise ValueError("classifier exploded")
        with self.assertRaisesRegex(ValueError, "exploded"):
            self.workflow(BrokenClassifier()).run(self.scan, frame=self.frame, run_store=self.store)
        self.assertEqual(len(self.events("atom_spectrum_acquired")), 1)
        self.assertEqual(len(self.events("atom_target_completed")), 0)
        names = [name for name, _ in self.backend.command_log]
        self.assertIn("BiasSpectr_Stop", names)
        self.assertIn("FolMe_Stop", names)
        self.assertIsNone(self.device.action_events.sink)

    def test_second_acquisition_failure_keeps_first_result_and_continues_all_stops(self):
        original = self.backend.BiasSpectr_Start
        calls = 0
        def failing(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second acquisition failed")
            return original(*args)
        self.backend.BiasSpectr_Start = failing
        with self.assertRaisesRegex(RuntimeError, "second acquisition"):
            self.workflow().run(self.scan, frame=self.frame, run_store=self.store)
        self.assertEqual(len(self.events("atom_target_completed")), 1)
        self.assertEqual(self.events("atom_campaign_failed")[0]["completed_targets"], 1)

    def test_frame_mismatch_and_live_backend_are_rejected_before_actions(self):
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.workflow().run(self.scan, frame=replace(self.frame, angle_deg=1), run_store=self.store)
        self.device = NanonisDevice(backend=object())
        with self.assertRaisesRegex(RuntimeError, "fake-only"):
            self.workflow().run(self.scan, frame=self.frame, run_store=self.store)
        self.assertNotIn("FolMe_XYPosSet", [name for name, _ in self.backend.command_log])

    def test_frame_change_after_first_spectrum_prevents_second_target(self):
        wrapped = self.workflow().classifier
        backend = self.backend
        class ChangingClassifier:
            def classify(self, data):
                backend.scan_frame = (*backend.scan_frame[:4], 1.0)
                return wrapped.classify(data)
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.workflow(ChangingClassifier()).run(self.scan, frame=self.frame, run_store=self.store)
        self.assertEqual(len(self.events("atom_spectrum_acquired")), 1)

    def test_deadline_after_acquisition_preserves_data_before_abort(self):
        original = self.backend.BiasSpectr_Start
        def slow(*args):
            time.sleep(.15)
            return original(*args)
        self.backend.BiasSpectr_Start = slow
        config = AtomSpectroscopyWorkflowConfig(maximum_duration_s=.14)
        with self.assertRaisesRegex(RuntimeError, "duration"):
            self.workflow(config=config).run(self.scan, frame=self.frame, run_store=self.store)
        self.assertEqual(len(self.events("atom_spectrum_acquired")), 1)

    def test_protective_stop_failure_does_not_skip_other_stop(self):
        class BrokenClassifier:
            def classify(self, _):
                raise ValueError("original failure")
        def fail_stop():
            raise RuntimeError("stop failed")
        self.device.stop_bias_spectroscopy = fail_stop
        with self.assertRaisesRegex(ValueError, "original failure"):
            self.workflow(BrokenClassifier()).run(self.scan, frame=self.frame, run_store=self.store)
        self.assertIn("FolMe_Stop", [name for name, _ in self.backend.command_log])
        self.assertEqual(len(self.events("atom_campaign_failed")[0]["stop_errors"]), 1)


class AcquisitionAndDetectionRegressionTests(unittest.TestCase):
    def test_frame_change_while_grabbing_data_rejects_acquisition(self):
        backend = FakeNanonisBackend(scan_running=False)
        original = backend.Scan_FrameDataGrab
        def changed(*args):
            result = original(*args)
            backend.scan_frame = (*backend.scan_frame[:4], 15.0)
            return result
        backend.Scan_FrameDataGrab = changed
        with self.assertRaisesRegex(Exception, "frame"):
            NanonisDevice(backend=backend).acquire_scan(0)

    def test_event_reader_paginates_without_creating_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                read_run_event_page(missing)
            self.assertFalse(missing.exists())
            store = RunStore.create(directory, signal_catalog={}, requested_period_ns=0)
            try:
                for i in range(3):
                    store.append_event("test_page", {"i": i}, monotonic_ns=i, utc_ns=i)
                first = read_run_event_page(store.database_path, limit=2)
                second = read_run_event_page(store.database_path, after_event_id=first["next_event_id"])
                self.assertEqual([e["payload"]["i"] for e in first["events"] + second["events"]], [0, 1, 2])
            finally:
                store.close()

    def test_down_scan_fetches_forward_data_and_preserves_source_frame(self):
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        result = device.acquire_scan(0, direction_up=False)
        self.assertEqual(result.configured_frame, device.get_scan_frame())
        self.assertEqual(result.frame_data.direction, "forward")
        self.assertEqual(result.frame_data.raw_scan_direction, 0)
        self.assertIn(("Scan_FrameDataGrab", (0, 1)), backend.command_log)

    def test_malformed_and_nonfinite_acquisition_metadata_raise(self):
        for mutation in ("names", "nonfinite"):
            backend = FakeNanonisBackend(scan_running=False)
            original = backend.BiasSpectr_Start
            def malformed(*args):
                result = list(original(*args))
                if mutation == "names":
                    result[2] = ["same", "same"]
                else:
                    result[5][1][1] = float("nan")
                return tuple(result)
            backend.BiasSpectr_Start = malformed
            with self.subTest(mutation=mutation), self.assertRaises(Exception) as error:
                NanonisDevice(backend=backend).acquire_bias_spectrum_transaction(spectroscopy_recipe())
            self.assertTrue(error.exception.acquired_evidence["response_received"])
            self.assertIn("raw_response_parts", error.exception.acquired_evidence)
            self.assertNotIn("ZCtrl_Withdraw", [name for name, _ in backend.command_log])

    def test_local_background_recovers_peak_hidden_by_large_terrace(self):
        y, x = np.indices((80, 80))
        image = (x > 25) * 10.0 + np.exp(-((x-50)**2 + (y-40)**2)/3)
        frame = ScanFrame(0., 0., 80e-9, 80e-9, 0.)
        local = LocalMaximumAtomDetector(AtomDetectorSettings(smoothing_radius_pixels=1,
                    background_radius_pixels=8, minimum_separation_pixels=5))
        result = local.detect(image, frame)
        self.assertTrue(any(abs(c.row-40) <= 1 and abs(c.column-50) <= 1 for c in result))


if __name__ == "__main__":
    unittest.main()
