"""Finite fake-only observer timing benchmark; never connects to hardware."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice
from workflow_layer.feedback_observation import observe_feedback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-s", type=float, default=10.)
    parser.add_argument("--read-delay-s", type=float, default=.004)
    args = parser.parse_args()
    if not 0 <= args.read_delay_s <= .1:
        parser.error("read-delay-s must be between 0 and .1")
    class DelayedFake(FakeNanonisBackend):
        def Signals_ValsGet(self, *values):
            time.sleep(args.read_delay_s)
            return super().Signals_ValsGet(*values)
    backend = DelayedFake(scan_running=False, z_setpoint_a=1e-10,
        signal_names=["Current (A)", "X (m)", "Y (m)", "Z (m)"],
        signal_values={0: 1.2e-10, 1: 0., 2: 0., 3: 0.})
    cpu = time.process_time(); started = time.perf_counter()
    with NanonisDevice(backend=backend) as device:
        result = observe_feedback(device, duration_s=args.duration_s, interval_s=.02,
            maximum_sample_gap_s=.5, maximum_abs_current_a=5e-10, z_margin_m=1e-9)
    elapsed = time.perf_counter()-started; cpu = time.process_time()-cpu
    rows = result["rows"]
    intervals = [(b[2]-a[2])/1e9 for a,b in zip(rows, rows[1:])]
    print(json.dumps({"backend": "fake_with_simulated_read_delay", "status": result["status"],
        "requested_duration_s": args.duration_s, "read_delay_s": args.read_delay_s,
        "samples": len(rows), "elapsed_including_analysis_s": elapsed, "process_cpu_s": cpu,
        "maximum_sample_gap_s": max(intervals, default=None), "timing": result["timing"],
        "live_timing_claim": False}, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__": raise SystemExit(main())
