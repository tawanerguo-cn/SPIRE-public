from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from instrument_layer import (
    BiasSpectroscopyAcquisition,
    ConditioningActionResult,
    FollowMeMoveResult,
    ScanFrame,
)


@dataclass(frozen=True)
class CoordinateTransform:
    raw_rows: int
    raw_columns: int
    scan_direction: str
    scan_frame: ScanFrame
    drift_offset_x_m: float = 0.0
    drift_offset_y_m: float = 0.0
    uncertainty_m: float = 0.0
    version: str = "nanonis-scan-frame-v1"


@dataclass(frozen=True)
class AtomSpectrumRecord:
    candidate_id: str
    x_m: float
    y_m: float
    detector_score: float
    classification: str
    classification_confidence: float
    classification_reason: str
    move: FollowMeMoveResult
    acquisition: BiasSpectroscopyAcquisition

    @property
    def spectrum(self):
        return self.acquisition.data


@dataclass(frozen=True)
class AtomMap:
    scan_channel_index: int
    scan_direction: str
    scan_frame: ScanFrame
    atoms: tuple[AtomSpectrumRecord, ...]
    coordinate_transform: CoordinateTransform | None = None
    detector_name: str = ""
    classifier_name: str = ""

    @property
    def unknown_count(self) -> int:
        return sum(atom.classification == "unknown" for atom in self.atoms)


class TipConditioningRunState(str, Enum):
    PRECHECK_REJECTED = "precheck_rejected"
    AWAITING_POSTCHECK = "awaiting_postcheck"
    SUCCEEDED = "succeeded"
    FAILED_POSTCHECK = "failed_postcheck"
    ABORTED = "aborted"


@dataclass(frozen=True)
class TipConditioningRunRecord:
    action_name: str
    state: TipConditioningRunState
    pre_score: float
    pre_confidence: float
    post_score: float | None = None
    post_confidence: float | None = None
    reason: str = ""
    site_x_m: float | None = None
    site_y_m: float | None = None
    move_result: FollowMeMoveResult | None = None
    action_result: ConditioningActionResult | None = None
