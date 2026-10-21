"""Saved spectroscopy inspection with explicit channels, axes and quality flags."""
from __future__ import annotations

from dataclasses import asdict
from analysis_layer import ReferenceSpectrumClassifier
from analysis_layer.spectrum_images import spectrum_channel_quality
from data_layer import read_nanonis_dat
from instrument_layer import BiasSpectroscopyData
from knowledge_base.spectrum_reference import load_spectrum_library


def inspect_saved_spectrum(path, *, channel: str, axis_channel: str = "Bias calc (V)",
                           library_path=None, conditions=None, maximum_distance=0.1,
                           minimum_margin=0.02) -> tuple[dict, BiasSpectroscopyData]:
    saved = read_nanonis_dat(path)
    spectrum = BiasSpectroscopyData(saved.columns, len(saved.columns), saved.data.shape[0],
                                   tuple(tuple(float(x) for x in row) for row in saved.data.T), ())
    references, library_hash = load_spectrum_library(library_path) if library_path else ((), None)
    classifier = ReferenceSpectrumClassifier(references, channel_name=channel, axis_channel_name=axis_channel,
                                            conditions=conditions, maximum_distance=maximum_distance,
                                            minimum_margin=minimum_margin)
    result = classifier.classify(spectrum)
    quality = spectrum_channel_quality(spectrum)
    return {"source_path": str(saved.path.resolve()), "source_sha256": saved.sha256,
            "columns": list(saved.columns), "header": dict(saved.header), "quality": quality,
            "selected_channel": channel, "axis_channel": axis_channel,
            "library_sha256": library_hash, "conditions_supplied": conditions or {},
            "classifier_settings": {"maximum_distance": maximum_distance, "minimum_margin": minimum_margin,
                                    "metric": "L2_distance_of_unit_norm_signals", "status": "baseline_not_lab_validated"},
            "classification": asdict(result), "authorizes_live_action": False,
            "notes": ["All-zero columns are flagged, not silently discarded or treated as acquired evidence.",
                      "Lock-in X in amperes is not automatically calibrated dI/dV in A/V.",
                      "Filename/time proximity does not associate this spectrum with a scan target."]}, spectrum


def render_spectrum(spectrum: BiasSpectroscopyData, *, channel: str, axis_channel: str) -> bytes:
    # Rendering is delegated to analysis; workflow only resolves the explicit channels.
    from analysis_layer.spectrum_images import render_spectrum_arrays
    x = spectrum.data[spectrum.channel_names.index(axis_channel)]
    y = spectrum.data[spectrum.channel_names.index(channel)]
    return render_spectrum_arrays(x, y, channel=channel, axis_channel=axis_channel)
