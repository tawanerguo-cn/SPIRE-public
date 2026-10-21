"""Read a bounded committed window via the existing data layer, without hardware."""
from contextlib import closing
import hashlib
import json
from pathlib import Path

from analysis_layer.signal_windows import analyze_signal_window
from data_layer.recording_signals import iter_recording_signals


def inspect_signal_window(database_path, start_seq, stop_seq, **analysis_options):
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (start_seq, stop_seq)):
        raise ValueError("sequence bounds must be integers")
    if not 0 <= start_seq <= stop_seq or stop_seq - start_seq + 1 > 100000:
        raise ValueError("request one window of at most 100000 samples")
    with closing(iter_recording_signals(database_path, minimum_seq=start_seq, maximum_seq=stop_seq)) as stream:
        rows = list(stream)
    # Nonfinite tokens are allowed only in this hash input, so invalid records still
    # get provenance. The returned report itself never contains nonfinite metrics.
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":"), allow_nan=True).encode()).hexdigest()
    result = analyze_signal_window(rows, **analysis_options)
    result.update(database_path=str(Path(database_path).resolve()), requested_start_seq=start_seq,
                  requested_stop_seq=stop_seq, source_rows_sha256=digest,
                  source_hash_encoding="python-json-rows-v1; compact; nonfinite-tokens")
    if not rows or rows[0][0] != start_seq or rows[-1][0] != stop_seq:
        result["status"] = "invalid_data"
        result["quality_flags"].append("requested_window_not_fully_committed")
    return result
