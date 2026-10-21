"""Reviewed observation patterns, separate from executable instrument recipes."""
from pathlib import Path
import tomllib


def load_tip_shaper_recording_pattern():
    path = Path(__file__).parent / "recording_patterns" / "tip_shaper_waveform_example.toml"
    with path.open("rb") as stream:
        return tomllib.load(stream)
