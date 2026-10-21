"""Analysis routines for scans, spectra, signals, and workflow outcomes.

This layer turns data into judgments such as tip quality, atom candidates, spectrum
fit results, or confidence scores. It must not operate instruments directly.
"""
from .atoms import (
    AtomCandidate,
    AtomDetector,
    AtomDetectorSettings,
    LocalMaximumAtomDetector,
    ReferenceSpectrumClassifier,
    SpectrumClassification,
    SpectrumClassifier,
    pixel_center_to_frame_fractions,
    pixel_center_to_xy,
)
from .tip_quality import (
    DeterministicTipQualityAnalyzer,
    TipQualityAnalyzer,
    TipQualityAssessment,
    TipQualityDecision,
    TipQualityThresholds,
)
from .tip_proximity import (
    TipProximityClassifier,
    TipProximityDecision,
    TipProximityObservation,
    TipProximityPolicy,
    TipProximityState,
)
from .tip_reference_verification import (
    Ag111SpectrumAnalysisConfig,
    Ag111SpectrumMetrics,
    CompactProtrusionAnalysisConfig,
    CompactProtrusionMetrics,
    analyze_ag111_spectrum,
    analyze_compact_protrusion,
)

__all__ = [
    "AtomCandidate",
    "AtomDetector",
    "AtomDetectorSettings",
    "DeterministicTipQualityAnalyzer",
    "LocalMaximumAtomDetector",
    "ReferenceSpectrumClassifier",
    "SpectrumClassification",
    "SpectrumClassifier",
    "TipQualityAnalyzer",
    "TipQualityAssessment",
    "TipQualityDecision",
    "TipQualityThresholds",
    "TipProximityClassifier",
    "TipProximityDecision",
    "TipProximityObservation",
    "TipProximityPolicy",
    "TipProximityState",
    "Ag111SpectrumAnalysisConfig",
    "Ag111SpectrumMetrics",
    "CompactProtrusionAnalysisConfig",
    "CompactProtrusionMetrics",
    "analyze_ag111_spectrum",
    "analyze_compact_protrusion",
    "pixel_center_to_frame_fractions",
    "pixel_center_to_xy",
]
