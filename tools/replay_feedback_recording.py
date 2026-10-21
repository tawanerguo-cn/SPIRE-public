"""Offline replay of recorded IXYZ through the actual control observer.

No live connection is constructed. Signal values and timing are unmodified;
feedback/working-point state is a stated replay assumption, not recovered data.
The input contains source-bound windows exported using data_layer readers.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice
from instrument_layer.models import SignalValue
from instrument_layer.signal_chart import SignalChartSample
from workflow_layer.feedback_observation import observe_feedback


def replay_window(rows, *, settling_s=0., overcurrent_hold_s=0., maximum_abs_current_a=5e-10,
                  withdraw_on_overcurrent=1, observation_mode="feedback", maximum_sample_gap_s=.06):
    if len(rows) < 2:
        return {"status": "insufficient_evidence", "reason": "fewer_than_two_samples"}
    duration = (rows[-1][2] - rows[0][2]) / 1e9
    if duration <= settling_s + overcurrent_hold_s + .02:
        return {"status": "insufficient_evidence", "reason": "window_too_short_for_policy"}
    if any(b[0] != a[0] + 1 or b[1] != a[1] or b[2] <= a[2] for a, b in zip(rows, rows[1:])):
        return {"status": "insufficient_evidence", "reason": "discontinuous_sequence_or_segment"}
    backend = FakeNanonisBackend(scan_running=False)
    backend.signal_names = ["Current (A)", "X (m)", "Y (m)", "Z (m)"]
    device = NanonisDevice(backend=backend)
    cursor = iter(rows)
    now = 0.
    def sample(_device, references, **_):
        nonlocal now
        row = next(cursor)
        now = (row[2] - rows[0][2]) / 1e9
        return SignalChartSample(now, [SignalValue(ref.index, ref.name, value)
                                      for ref, value in zip(references, row[4:8])])
    with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=sample):
        result = observe_feedback(device, duration_s=min(duration, 600.), interval_s=.02,
            maximum_sample_gap_s=maximum_sample_gap_s, maximum_abs_current_a=maximum_abs_current_a,
            z_margin_m=1e-9, settling_s=settling_s, overcurrent_hold_s=overcurrent_hold_s,
            withdraw_on_overcurrent=withdraw_on_overcurrent,
            observation_mode=observation_mode, finished=lambda: now >= duration,
            clock=lambda: now, utc_ns=lambda: rows[0][3] + round(now * 1e9), wait=lambda _: None)
    return {"status": result["status"], "error": result.get("error"),
        "elapsed_s": now, "read_samples": len(result["rows"]),
        "current_check": result["current_check"],
        "protective_withdraw": any(name == "ZCtrl_Withdraw" for name, _ in backend.command_log),
        "source_rows_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
        "observation_mode": observation_mode,
        "assumptions": "Only recorded IXYZ and timing replayed; control states are fake (ON, fixed bias/setpoint, idle scan/approach). Spectroscopy mode permits HOLD/changing bias; no measured state reconstruction or hardware latency claim. Not tip-quality acceptance."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("windows", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--settling-s", type=float, default=3.)
    p.add_argument("--overcurrent-hold-s", type=float, default=.5)
    args = p.parse_args()
    windows = json.loads(args.windows.read_text(encoding="utf-8"))
    result = [{"metadata": item["metadata"],
               "old": replay_window(item["rows"]),
               "candidate": replay_window(item["rows"], settling_s=args.settling_s,
                                           overcurrent_hold_s=args.overcurrent_hold_s,
                                           withdraw_on_overcurrent=0)} for item in windows]
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    from collections import Counter
    print(json.dumps({key: dict(Counter(item[key]["status"] for item in result))
                      for key in ("old", "candidate")}))


if __name__ == "__main__":
    main()
