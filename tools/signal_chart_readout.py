from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrument_layer import NanonisConnectionConfig, NanonisDevice, benchmark_signal_polling, load_connection_config
from instrument_layer.signal_chart import sample_signal_chart


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replicate the Nanonis Signal Chart readout for selected Signals Manager channels."
    )
    parser.add_argument("--live", action="store_true", help="Connect to live Nanonis. Default uses fake backend.")
    parser.add_argument("--host", help="Override Nanonis host.")
    parser.add_argument("--port", type=int, help="Override Nanonis TCP port.")
    parser.add_argument("--timeout-s", type=float, help="Override connection timeout.")
    parser.add_argument("--signal", action="append", dest="signals", help="Signal name or index. Repeatable.")
    parser.add_argument("--interval-s", type=float, default=0.02, help="Acquisition interval in seconds. Use 0 for max speed.")
    parser.add_argument("--draw-interval-s", type=float, default=1 / 30, help="Plot refresh interval in seconds.")
    parser.add_argument("--points", type=int, default=500, help="Number of points visible in the chart.")
    parser.add_argument("--duration-s", type=float, help="Optional duration before exiting.")
    parser.add_argument("--benchmark", action="store_true", help="Measure raw Signals.ValsGet polling throughput.")
    parser.add_argument("--wait-newest", action="store_true", help="Ask Nanonis to wait for newest signal values.")
    parser.add_argument("--print-only", action="store_true", help="Print values without opening a plot window.")
    parser.add_argument("--once", action="store_true", help="Read one sample and exit.")
    args = parser.parse_args(argv)

    config = _override_config(load_connection_config(), args.host, args.port, args.timeout_s)
    signals = _parse_signals(args.signals or ["Current (A)", "Z (m)"])

    try:
        device = NanonisDevice(config=config, live=args.live)
    except OSError as exc:
        print(
            f"failed to connect to Nanonis at {config.host}:{config.port}: {exc}\n"
            "Check that the Nanonis Programming Interface/TCP server is enabled and that the port is correct.",
            file=sys.stderr,
        )
        return 10
    except RuntimeError as exc:
        print(f"failed to initialize Nanonis backend: {exc}", file=sys.stderr)
        return 11

    with device:
        references = device.resolve_signals(signals)
        _print_resolution(references, live=args.live, config=config)
        unresolved = [reference for reference in references if reference.error is not None]
        if unresolved:
            return 2

        if args.benchmark:
            return _run_benchmark(device, references, args.duration_s or 2.0, args.wait_newest)

        if args.once:
            sample = sample_signal_chart(device, references, wait_newest=args.wait_newest)
            print(_format_sample(sample.values))
            return 0

        if args.print_only:
            return _run_text_loop(device, references, args.interval_s, args.duration_s, args.wait_newest)

        return _run_plot_loop(
            device,
            references,
            args.interval_s,
            args.draw_interval_s,
            args.points,
            args.duration_s,
            args.wait_newest,
        )


def _override_config(
    config: NanonisConnectionConfig,
    host: str | None,
    port: int | None,
    timeout_s: float | None,
) -> NanonisConnectionConfig:
    return NanonisConnectionConfig(
        host=host or config.host,
        port=port if port is not None else config.port,
        timeout_s=timeout_s if timeout_s is not None else config.timeout_s,
        backend=config.backend,
    )


def _parse_signals(signals: list[str]) -> list[str | int]:
    parsed: list[str | int] = []
    for signal in signals:
        try:
            parsed.append(int(signal))
        except ValueError:
            parsed.append(signal)
    return parsed


def _print_resolution(references, *, live: bool, config: NanonisConnectionConfig) -> None:
    mode = "live" if live else "fake"
    print(f"backend={mode} host={config.host} port={config.port}")
    for reference in references:
        if reference.error:
            print(f"UNRESOLVED requested={reference.requested!r} error={reference.error}")
        else:
            print(f"resolved requested={reference.requested!r} index={reference.index} name={reference.name!r}")


def _run_text_loop(device, references, interval_s: float, duration_s: float | None, wait_newest: bool) -> int:
    start = time.perf_counter()
    try:
        while duration_s is None or time.perf_counter() - start < duration_s:
            sample = sample_signal_chart(device, references, wait_newest=wait_newest)
            print(f"{sample.timestamp_s:.3f} {_format_sample(sample.values)}")
            time.sleep(max(0.0, interval_s))
    except KeyboardInterrupt:
        return 130
    return 0


def _run_benchmark(device, references, duration_s: float, wait_newest: bool) -> int:
    acq_period_s = _safe_acquisition_period(device)
    if acq_period_s is not None:
        print(f"nanonis_acquisition_period_s={acq_period_s:.9g} theoretical_rate_hz={1 / acq_period_s:.3g}")
    result = benchmark_signal_polling(
        device,
        references,
        duration_s=duration_s,
        wait_newest=wait_newest,
    )
    print(f"samples={result.samples}")
    print(f"duration_s={result.duration_s:.6f}")
    print(f"samples_per_s={result.samples_per_s:.3f}")
    if result.mean_interval_s is not None:
        print(f"mean_interval_ms={result.mean_interval_s * 1e3:.3f}")
        print(f"min_interval_ms={result.min_interval_s * 1e3:.3f}")
        print(f"max_interval_ms={result.max_interval_s * 1e3:.3f}")
    return 0


def _run_plot_loop(
    device,
    references,
    interval_s: float,
    draw_interval_s: float,
    points: int,
    duration_s: float | None,
    wait_newest: bool,
) -> int:
    import matplotlib.pyplot as plt

    names = [reference.name or str(reference.requested) for reference in references]
    scales = [_display_scale(name) for name in names]
    colors = ["#55f0a8", "#f0b72f", "#5dade2", "#f06f6f"]
    history_t = deque(maxlen=points)
    histories = [deque(maxlen=points) for _ in references]
    lock = Lock()
    stop_event = Event()
    start = time.perf_counter()

    def acquire() -> None:
        while not stop_event.is_set() and (duration_s is None or time.perf_counter() - start < duration_s):
            sample = sample_signal_chart(device, references, wait_newest=wait_newest)
            with lock:
                history_t.append(sample.timestamp_s - start)
                for history, value, (scale, _) in zip(histories, sample.values, scales, strict=False):
                    history.append(float("nan") if value.value is None else value.value * scale)
            if interval_s > 0:
                stop_event.wait(interval_s)

    worker = Thread(target=acquire, name="signal-chart-acquisition", daemon=True)
    worker.start()

    fig, axes = plt.subplots(len(references), 1, sharex=True, figsize=(10, 5))
    if len(references) == 1:
        axes = [axes]
    fig.canvas.manager.set_window_title("Nanonis Signal Chart Readout")
    lines = []
    for axis, name, (scale, unit_label), color in zip(axes, names, scales, colors, strict=False):
        axis.set_facecolor("#050505")
        axis.grid(True, color="#303030", linewidth=0.6)
        axis.tick_params(colors="#222222")
        axis.set_ylabel(f"{name} [{unit_label}]", color="#222222")
        (line,) = axis.plot([], [], color=color, linewidth=1.0)
        lines.append(line)
    axes[-1].set_xlabel("time (s)")
    fig.tight_layout()
    plt.show(block=False)

    try:
        while plt.fignum_exists(fig.number) and (duration_s is None or time.perf_counter() - start < duration_s):
            with lock:
                times = list(history_t)
                series = [list(history) for history in histories]
            for axis, line, values in zip(axes, lines, series, strict=False):
                line.set_data(times, values)
                axis.relim()
                axis.autoscale_view()
            fig.canvas.draw_idle()
            plt.pause(max(0.001, draw_interval_s))
    except KeyboardInterrupt:
        return 130
    finally:
        stop_event.set()
        worker.join(timeout=1.0)
    return 0


def _format_sample(values) -> str:
    parts = []
    for value in values:
        if value.error:
            parts.append(f"{value.name or value.index}=ERROR:{value.error}")
        else:
            scale, unit_label = _display_scale(value.name)
            parts.append(f"{value.name or value.index}={value.value * scale:.6g} {unit_label}")
    return " ".join(parts)


def _display_scale(name: str | None) -> tuple[float, str]:
    if name == "Current (A)":
        return 1e12, "pA"
    if name == "Z (m)":
        return 1e9, "nm"
    if name and name.endswith("(A)"):
        return 1e12, "pA"
    if name and name.endswith("(m)"):
        return 1e9, "nm"
    return 1.0, "raw"


def _safe_acquisition_period(device) -> float | None:
    try:
        return device.get_acquisition_period_s()
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
