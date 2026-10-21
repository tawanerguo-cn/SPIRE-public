"""Explicitly reviewed spectral priors; measurements never promote themselves."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class SpectrumReference:
    reference_id: str
    label: str
    channel_name: str
    bias_v: tuple[float, ...]
    values: tuple[float, ...]
    conditions: tuple[tuple[str, str], ...]
    status: str
    reviewed_by: str
    source: str

    def __post_init__(self):
        if not all((self.reference_id, self.label, self.channel_name, self.reviewed_by, self.source)):
            raise ValueError("reference requires identity, channel with units, reviewer and source")
        if self.label == "unknown" or self.status not in {"reviewed", "synthetic"}:
            raise ValueError("only reviewed or explicitly synthetic references are usable")
        if len(self.bias_v) < 3 or len(self.bias_v) != len(self.values):
            raise ValueError("reference requires at least three paired bias/value samples")
        if not all(math.isfinite(x) for x in (*self.bias_v, *self.values)):
            raise ValueError("reference data must be finite")
        differences = [b - a for a, b in zip(self.bias_v, self.bias_v[1:])]
        if not (all(d > 0 for d in differences) or all(d < 0 for d in differences)):
            raise ValueError("reference axis must be strictly monotonic")
        if max(self.values) == min(self.values):
            raise ValueError("constant references do not support shape classification")
        keys = [k for k, _ in self.conditions]
        if len(set(keys)) != len(keys) or any(not k or not v for k, v in self.conditions):
            raise ValueError("reference conditions must be unique and nonempty")
        if self.status == "reviewed" and not self.conditions:
            raise ValueError("reviewed references require explicit applicability conditions")


def load_spectrum_library(path: str | Path) -> tuple[tuple[SpectrumReference, ...], str]:
    payload = Path(path).read_bytes()
    document = json.loads(payload)
    if set(document) != {"schema_version", "references"} or document["schema_version"] != 1:
        raise ValueError("unsupported spectral library schema")
    references = []
    for item in document["references"]:
        fields = dict(item)
        fields["bias_v"] = tuple(fields["bias_v"])
        fields["values"] = tuple(fields["values"])
        fields["conditions"] = tuple(sorted(fields["conditions"].items()))
        references.append(SpectrumReference(**fields))
    if len({r.reference_id for r in references}) != len(references):
        raise ValueError("duplicate spectral reference id")
    return tuple(references), sha256(payload).hexdigest()
