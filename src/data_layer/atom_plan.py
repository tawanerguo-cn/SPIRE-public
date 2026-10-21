"""Serializable target evidence. A plan is never a live movement authorization."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

from instrument_layer import ScanFrame


@dataclass(frozen=True)
class AtomTarget:
    target_id: str
    row: int
    column: int
    x_m: float
    y_m: float
    pixel_uncertainty_m: float
    selected_by: str
    rationale: str


@dataclass(frozen=True)
class AtomTargetPlan:
    observation_id: str
    source_path: str
    source_sha256: str
    channel: str
    processing: str
    frame: ScanFrame
    rows: int
    columns: int
    targets: tuple[AtomTarget, ...]
    excluded_regions: tuple[tuple[int, int, int, int], ...] = ()
    execution_mode: str = "fake_only"
    schema_version: int = 1

    @property
    def plan_id(self) -> str:
        return "plan-" + sha256(_encode(asdict(self)).encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        return asdict(self) | {"plan_id": self.plan_id, "authorizes_live_action": False}


def _encode(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def write_json_artifact(path: str | Path, value: dict) -> Path:
    """Exclusive file creation; encode first so invalid data creates no artifact."""
    serialized = _encode(value)
    destination = Path(path).resolve()
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(serialized + "\n")
        stream.flush()
        __import__("os").fsync(stream.fileno())
    return destination


def read_atom_plan(path: str | Path) -> AtomTargetPlan:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    identity = value.pop("plan_id")
    if value.pop("authorizes_live_action") is not False:
        raise ValueError("saved plans cannot authorize live actions")
    value["frame"] = ScanFrame(**value["frame"])
    value["targets"] = tuple(AtomTarget(**target) for target in value["targets"])
    value["excluded_regions"] = tuple(tuple(region) for region in value["excluded_regions"])
    plan = AtomTargetPlan(**value)
    if plan.schema_version != 1 or plan.execution_mode != "fake_only" or plan.plan_id != identity:
        raise ValueError("unsupported or modified atom plan")
    return plan
