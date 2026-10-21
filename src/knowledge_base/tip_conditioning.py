"""Read operator guidance and a selectable stage catalog, never executable recipes."""
from pathlib import Path
import tomllib


def load_tip_conditioning_knowledge() -> dict:
    path = Path(__file__).parent / "tip_conditioning" / "local_experience.toml"
    with path.open("rb") as stream:
        library = tomllib.load(stream)
    if library["library"]["executable"] is not False:
        raise ValueError("tip-conditioning experience must not authorize execution")
    entries = library["entries"]
    if len({entry["id"] for entry in entries}) != len(entries):
        raise ValueError("duplicate tip-conditioning knowledge entry")
    workflow = library.get("workflow")
    if workflow is not None:
        if workflow.get("executable") is not False:
            raise ValueError("conditioning workflow knowledge cannot authorize execution")
        stage_ids = [stage["id"] for stage in workflow["stages"]]
        if len(set(stage_ids)) != len(stage_ids) or workflow["entry"] not in stage_ids:
            raise ValueError("conditioning stage catalog requires unique ids and a known entry")
    return library


def load_tip_quality_rubric() -> dict:
    path = Path(__file__).parent / "tip_conditioning" / "quality_rating.toml"
    with path.open("rb") as stream:
        return tomllib.load(stream)
