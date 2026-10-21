"""Observation presentation composition shared by runtime transports."""
from analysis_layer import AtomCandidate
from analysis_layer.scan_images import render_scan_observation
from data_layer.scan_observation import write_observation_bundle


def render_observation(observation, *, candidates=(), targets=(), crop=None):
    if targets:
        candidates = tuple(AtomCandidate(t.target_id, t.row, t.column, t.x_m, t.y_m,
                          float(observation.processed[t.row, t.column]), 0.0) for t in targets)
    return render_scan_observation(observation, candidates=candidates, crop=crop)


def export_agent_observation(observation, summary, path, preview=None):
    return write_observation_bundle(observation, path, summary=summary,
                                   images={"overview.png": preview} if preview else {})
