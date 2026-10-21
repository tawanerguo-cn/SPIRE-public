from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable

from analysis_layer import AtomCandidate, AtomDetector, SpectrumClassifier, pixel_center_to_xy
from data_layer import AtomMap, AtomSpectrumRecord, CoordinateTransform, RunStore
from instrument_layer import BiasSpectroscopyRecipe, NanonisDevice, ScanFrame, ScanFrameData
from instrument_layer.models import to_jsonable


@dataclass(frozen=True)
class AtomSpectroscopyWorkflowConfig:
    maximum_atoms: int = 20
    maximum_duration_s: float = 300.0


class AtomSpectroscopyWorkflow:
    """Detect atoms in one completed scan, move to each, acquire, classify, and record."""

    def __init__(
        self,
        device: NanonisDevice,
        detector: AtomDetector,
        classifier: SpectrumClassifier,
        recipe: BiasSpectroscopyRecipe,
        config: AtomSpectroscopyWorkflowConfig | None = None,
    ) -> None:
        self.device = device
        self.detector = detector
        self.classifier = classifier
        self.recipe = recipe
        self.config = config or AtomSpectroscopyWorkflowConfig()

    def run(
        self, scan: ScanFrameData, *, frame: ScanFrame, run_store: RunStore,
        targets: tuple[AtomCandidate, ...] | None = None,
        source: dict | None = None, check_ready: Callable[[], None] | None = None,
    ) -> AtomMap:
        """Execute one persisted fake campaign on explicitly top-origin pixels.

        Source-bound saved plans use this same loop. Raw API image orientation,
        drift and live spectroscopy remain uncommissioned, so no live execution
        is accepted even if an unrelated SafetyGate policy later changes.
        """
        if not self.device.is_fake_backend:
            raise RuntimeError("atom spectroscopy workflow is fake-only pending commissioning")
        budget = self.device.validate_control_action(
            "recipe_execution",
            {
                "max_steps": self.config.maximum_atoms,
                "max_duration_s": self.config.maximum_duration_s,
            },
            require_live_enabled=not self.device.is_fake_backend,
        )
        budget.raise_for_violations()
        self.device.validate_bias_spectroscopy_recipe(self.recipe).raise_for_violations()
        if self.device.get_scan_status().running:
            raise RuntimeError("atom spectroscopy requires a completed/stopped scan")
        if scan.rows <= 0 or scan.columns <= 0:
            raise ValueError("scan metadata must contain positive rows and columns")
        if len(scan.data) != scan.rows or any(len(row) != scan.columns for row in scan.data):
            raise ValueError("scan data shape does not match its rows/columns metadata")
        if self.device.get_scan_frame() != frame:
            raise RuntimeError("scan frame differs from source-bound target frame")
        candidates = targets if targets is not None else self.detector.detect(scan.data, frame)
        if not candidates:
            raise ValueError("no selected targets")
        if len(candidates) > self.config.maximum_atoms:
            raise RuntimeError(
                f"detector returned {len(candidates)} candidates; workflow limit is {self.config.maximum_atoms}"
            )

        if len({c.candidate_id for c in candidates}) != len(candidates):
            raise ValueError("duplicate target identities")
        for candidate in candidates:
            expected = pixel_center_to_xy(frame, scan.rows, scan.columns, candidate.row, candidate.column)
            if expected != (candidate.x_m, candidate.y_m):
                raise ValueError("candidate coordinates do not match source pixel")
            if not math.isfinite(scan.data[candidate.row][candidate.column]):
                raise ValueError("target pixel is unacquired")
        if len(candidates) * self.recipe.estimated_duration_s > self.config.maximum_duration_s:
            raise RuntimeError("planned spectra exceed workflow duration budget")

        def event(name, payload):
            run_store.append_event(name, to_jsonable(payload), monotonic_ns=time.perf_counter_ns(), utc_ns=time.time_ns())

        def ready():
            if check_ready is not None:
                check_ready()
            if time.perf_counter() - started_at > self.config.maximum_duration_s:
                raise RuntimeError("atom spectroscopy exceeded its bounded duration budget")
            if self.device.get_scan_frame() != frame:
                raise RuntimeError("scan frame changed during atom campaign")
            if self.device.get_scan_status().running:
                raise RuntimeError("scan restarted during atom campaign")

        records: list[AtomSpectrumRecord] = []
        started_at = time.perf_counter()
        event("atom_campaign_planned", {"source": source or {"kind": "synthetic_test_grid"},
              "frame": frame, "targets": candidates, "recipe": self.recipe, "config": self.config,
              "mode": "fake_only", "classification_score_kind": "similarity_not_probability"})
        previous_sink = self.device.action_events.set_sink(run_store.append_action_event)
        candidate = None
        try:
            for candidate in candidates:
                ready()
                if time.perf_counter() - started_at + self.recipe.estimated_duration_s > self.config.maximum_duration_s:
                    raise RuntimeError("insufficient remaining duration for next spectrum")
                event("atom_target_started", {"target_id": candidate.candidate_id})
                move = self.device.move_tip_xy(candidate.x_m, candidate.y_m, wait=True)
                event("atom_target_positioned", {"target_id": candidate.candidate_id, "move": move})
                ready()
                acquisition = self.device.acquire_bias_spectrum_transaction(self.recipe)
                # Commit the full raw transaction BEFORE any classifier can fail.
                event("atom_spectrum_acquired", {"target_id": candidate.candidate_id, "acquisition": acquisition})
                ready()
                classification = self.classifier.classify(acquisition.data)
                records.append(
                    AtomSpectrumRecord(
                        candidate_id=candidate.candidate_id,
                        x_m=candidate.x_m,
                        y_m=candidate.y_m,
                        detector_score=candidate.score,
                        classification=classification.label,
                        classification_confidence=classification.confidence,
                        classification_reason=classification.reason,
                        move=move,
                        acquisition=acquisition,
                    )
                )
                event("atom_target_completed", {"record": records[-1], "classification_evidence": classification})
        except BaseException as error:
            stop_errors = []
            for stop in (self.device.stop_bias_spectroscopy, self.device.stop_follow_me):
                try:
                    stop()
                except Exception as stop_error:
                    stop_errors.append(f"{stop.__name__}: {stop_error}")
            event("atom_campaign_failed", {"target_id": candidate.candidate_id if candidate else None,
                  "completed_targets": len(records), "error_type": type(error).__name__,
                  "error": str(error), "stop_errors": stop_errors})
            raise
        finally:
            self.device.action_events.set_sink(previous_sink)

        atom_map = AtomMap(
            scan_channel_index=scan.channel_index,
            scan_direction=scan.direction,
            scan_frame=frame,
            atoms=tuple(records),
            coordinate_transform=CoordinateTransform(
                raw_rows=scan.rows,
                raw_columns=scan.columns,
                scan_direction=scan.direction,
                scan_frame=frame,
                uncertainty_m=0.5
                * math.hypot(frame.width_m / scan.columns, frame.height_m / scan.rows),
            ),
            detector_name=type(self.detector).__name__,
            classifier_name=type(self.classifier).__name__,
        )
        event("atom_campaign_completed", {"atom_map": atom_map})
        return atom_map

    def acquire_and_run(
        self,
        channel_index: int,
        *,
        direction_up: bool = True,
        scan_timeout_ms: int = 120000,
        run_store: RunStore,
    ) -> AtomMap:
        """Acquire a fresh configured scan, then execute the same audited atom pipeline."""

        if not self.device.is_fake_backend:
            raise RuntimeError("atom spectroscopy workflow is fake-only pending commissioning")
        scan = self.device.acquire_scan(
            channel_index,
            direction_up=direction_up,
            timeout_ms=scan_timeout_ms,
        )
        if scan.configured_frame is None:
            raise RuntimeError("acquisition lacks source frame")
        return self.run(scan.frame_data, frame=scan.configured_frame, run_store=run_store)
