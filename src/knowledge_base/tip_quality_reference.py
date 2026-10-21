from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import string
import tomllib


DEFAULT_TIP_QUALITY_REFERENCE_DIRECTORY = (
    Path(__file__).resolve().parent / "tip_quality_references"
)
DEFAULT_TIP_QUALITY_REFERENCE_ID = "ag111_short_scan_reference"


MetricValue = str | int | float | bool


class PrivateReferenceSourcesUnavailable(ValueError):
    """The historical reference metadata is present but raw files were withheld."""


@dataclass(frozen=True)
class TipQualityEvidence:
    evidence_id: str
    kind: str
    role: str
    observed_at: str
    source_path: PurePath | None
    sha256: str | None
    metrics: tuple[tuple[str, MetricValue], ...]
    notes: str

    def metric(self, name: str) -> MetricValue:
        for metric_name, value in self.metrics:
            if metric_name == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True)
class TipQualityLiteratureAnchor:
    doi: str
    url: str
    fact: str


@dataclass(frozen=True)
class TipQualityReference:
    reference_id: str
    status: str
    reviewed_on: str
    station_profile_id: str
    surface: str
    sample_id: str
    verdict: str
    targets: tuple[str, ...]
    operator_summary: str
    operator_quick_judgment_rule: str
    outcome_scope: str
    recipe_inference_permitted: bool
    source_available: bool
    why_good: tuple[str, ...]
    limitations: tuple[str, ...]
    run_id: str | None
    segment_id: int
    database_path: PurePath | None
    database_sha256: str | None
    recorded_signals: tuple[str, ...]
    requested_rate_hz: float
    signal_samples: int
    estimated_missing_samples: int
    coverage_fraction: float
    action_identity: str
    action_identity_reason: str
    evidence: tuple[TipQualityEvidence, ...]
    literature: tuple[TipQualityLiteratureAnchor, ...]


def load_tip_quality_reference(
    reference_id: str = DEFAULT_TIP_QUALITY_REFERENCE_ID,
    *,
    reference_directory: str | Path = DEFAULT_TIP_QUALITY_REFERENCE_DIRECTORY,
) -> TipQualityReference:
    _validate_identifier(reference_id)
    path = Path(reference_directory) / f"{reference_id}.toml"
    with path.open("rb") as handle:
        payload = tomllib.load(handle)

    reference = _table(payload, "reference")
    provenance = _table(payload, "provenance")
    time_series = _table(payload, "time_series")
    if str(reference.get("id")) != reference_id:
        raise ValueError(f"{path} reference.id does not match its requested id")
    if str(reference.get("status")) != "reviewed":
        raise ValueError(f"tip-quality reference {reference_id!r} is not reviewed")
    if bool(reference.get("recipe_inference_permitted")):
        raise ValueError("an outcome reference cannot authorize recipe inference")

    targets = tuple(str(value) for value in reference.get("targets", ()))
    why_good = tuple(str(value) for value in reference.get("why_good", ()))
    limitations = tuple(str(value) for value in reference.get("limitations", ()))
    if not targets or not why_good or not limitations:
        raise ValueError(
            "tip-quality reference requires targets, why_good, and limitations"
        )

    source_available = _strict_bool(
        reference.get("source_available", True), "reference.source_available"
    )
    database_path = _source_path(
        provenance.get("database_path"),
        "provenance.database_path",
        source_available=source_available,
    )
    database_sha256 = _sha256(
        provenance.get("database_sha256"),
        "database",
        source_available=source_available,
    )

    requested_rate_hz = float(time_series.get("requested_rate_hz"))
    signal_samples = int(time_series.get("signal_samples"))
    estimated_missing_samples = int(
        time_series.get("estimated_missing_samples")
    )
    coverage_fraction = float(time_series.get("coverage_fraction"))
    if requested_rate_hz <= 0.0 or signal_samples <= 0:
        raise ValueError("time-series rate and sample count must be positive")
    if estimated_missing_samples < 0 or not 0.0 <= coverage_fraction <= 1.0:
        raise ValueError("invalid time-series completeness metadata")

    evidence = _evidence(
        payload.get("evidence"), source_available=source_available
    )
    literature = _literature(payload.get("literature"))
    return TipQualityReference(
        reference_id=reference_id,
        status="reviewed",
        reviewed_on=str(reference.get("reviewed_on")),
        station_profile_id=str(reference.get("station_profile_id")),
        surface=str(reference.get("surface")),
        sample_id=str(reference.get("sample_id")),
        verdict=str(reference.get("verdict")),
        targets=targets,
        operator_summary=str(reference.get("operator_summary")),
        operator_quick_judgment_rule=str(
            reference.get("operator_quick_judgment_rule")
        ),
        outcome_scope=str(reference.get("outcome_scope")),
        recipe_inference_permitted=False,
        source_available=source_available,
        why_good=why_good,
        limitations=limitations,
        run_id=(
            str(provenance["run_id"])
            if source_available and provenance.get("run_id") is not None
            else None
        ),
        segment_id=int(provenance.get("segment_id")),
        database_path=database_path,
        database_sha256=database_sha256,
        recorded_signals=tuple(
            str(value) for value in time_series.get("recorded_signals", ())
        ),
        requested_rate_hz=requested_rate_hz,
        signal_samples=signal_samples,
        estimated_missing_samples=estimated_missing_samples,
        coverage_fraction=coverage_fraction,
        action_identity=str(provenance.get("action_identity")),
        action_identity_reason=str(
            provenance.get("action_identity_reason")
        ),
        evidence=evidence,
        literature=literature,
    )


def _evidence(value, *, source_available: bool) -> tuple[TipQualityEvidence, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("tip-quality reference must contain evidence")
    result: list[TipQualityEvidence] = []
    identifiers: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("evidence entries must be tables")
        evidence_id = str(entry.get("id"))
        _validate_identifier(evidence_id)
        if evidence_id in identifiers:
            raise ValueError(f"duplicate evidence id {evidence_id!r}")
        identifiers.add(evidence_id)
        source_path = _source_path(
            entry.get("source_path"),
            f"evidence {evidence_id!r} source_path",
            source_available=source_available,
        )
        metrics = entry.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ValueError(f"evidence {evidence_id!r} metrics must be a table")
        metric_items: list[tuple[str, MetricValue]] = []
        for name, metric_value in sorted(metrics.items()):
            if not isinstance(metric_value, (str, int, float, bool)):
                raise ValueError(
                    f"evidence {evidence_id!r} metric {name!r} must be scalar"
                )
            metric_items.append((str(name), metric_value))
        result.append(
            TipQualityEvidence(
                evidence_id=evidence_id,
                kind=str(entry.get("kind")),
                role=str(entry.get("role")),
                observed_at=str(entry.get("observed_at")),
                source_path=source_path,
                sha256=_sha256(
                    entry.get("sha256"),
                    evidence_id,
                    source_available=source_available,
                ),
                metrics=tuple(metric_items),
                notes=str(entry.get("notes", "")),
            )
        )
    return tuple(result)


def _source_path(
    value, label: str, *, source_available: bool
) -> PurePath | None:
    """Archived provenance may belong to another OS; do not resolve it locally."""
    if not source_available:
        if value is not None:
            raise ValueError(f"{label} must be omitted when source_available is false")
        return None
    if isinstance(value, str) and value:
        for kind in (PurePosixPath, PureWindowsPath):
            path = kind(value)
            if path.is_absolute():
                return path
    raise ValueError(f"{label} must be absolute")


def _literature(value) -> tuple[TipQualityLiteratureAnchor, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("tip-quality reference requires a literature anchor")
    return tuple(
        TipQualityLiteratureAnchor(
            doi=str(entry.get("doi")),
            url=str(entry.get("url")),
            fact=str(entry.get("fact")),
        )
        for entry in value
        if isinstance(entry, dict)
    )


def _sha256(value, label: str, *, source_available: bool) -> str | None:
    if not source_available:
        if value is not None:
            raise ValueError(
                f"{label} SHA-256 must be omitted when source_available is false"
            )
        return None
    digest = str(value).lower()
    if len(digest) != 64 or any(character not in string.hexdigits for character in digest):
        raise ValueError(f"{label} must have a valid SHA-256 digest")
    return digest


def _strict_bool(value, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a TOML boolean")
    return value


def _table(payload: dict, key: str) -> dict:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid [{key}] table")
    return value


def _validate_identifier(identifier: str) -> None:
    if not identifier or len(identifier) > 80:
        raise ValueError("reference identifiers must contain 1-80 characters")
    if any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in identifier
    ):
        raise ValueError("reference identifier contains unsafe path characters")
