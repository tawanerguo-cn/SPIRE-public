from __future__ import annotations

import argparse
from contextlib import ExitStack
import re
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_layer import ExistingRunActionEventSink
from instrument_layer import (
    NanonisConnectionConfig,
    NanonisDevice,
)
from instrument_layer.models import to_jsonable
from instrument_layer.control_authorization import load_control_authorization
from workflow_layer.control_step import execute_control_step
from workflow_layer.feedback_observation import observe_feedback
from runtime_layer.control_requests import load_control_session, read_control_json, resolve_control_request


def build_step_request(*, request_file=None, action=None, params=(), purpose=None, preset=None, quick=None, session=None):
    """Decode CLI input only; all action validation/execution stays in control_step."""
    if sum(bool(v) for v in (request_file, action, preset, quick)) != 1:
        raise ValueError("Choose one request file, action, preset or short instruction")
    if request_file is not None:
        if params or purpose is not None:
            raise ValueError("Do not mix --request-file with --action, --param or --purpose")
        return resolve_control_request(read_control_json(request_file), session)
    if quick:
        if params or purpose is not None:
            raise ValueError("Short instructions use saved purpose and positional parameters")
        try:
            tokens = [quick[0]] + [json.loads(item) for item in quick[1:]]
        except json.JSONDecodeError:
            raise ValueError("Short instruction coordinates and sizes must be JSON numbers") from None
        return resolve_control_request(tokens, session)
    if action and (not purpose or not purpose.strip()):
        raise ValueError("Parameterized steps require --action and a nonempty --purpose")
    arguments = {}
    for item in params:
        key, separator, raw = item.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not raw:
            raise ValueError("Each --param must be name=value")
        if key in arguments:
            raise ValueError(f"Duplicate parameter: {key}")
        try:
            value = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite parameter: " + value)))
        except json.JSONDecodeError:
            # Common enum values such as negative/engaged need no shell JSON quotes.
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", raw):
                raise ValueError(f"Invalid JSON or bare enum for {key}") from None
            value = raw
        arguments[key] = value
    if preset:
        value = {"preset": preset, "arguments": arguments}
        if purpose is not None:
            value["purpose"] = purpose
    else:
        value = {"action": action, "arguments": arguments, "purpose": purpose}
    return resolve_control_request(value, session)


def step_outcome(action, result):
    """Propagate composite workflow failures; a returned dict is not success."""
    value = result.get('result') or {}
    if action == 'home_z_controller':
        return value['motion_completion'], 2
    if action in {'observe_noise', 'inspect_lock_in_configuration'}:
        status = value.get('status', 'unknown')
        return (status, 0 if status == 'completed' else 2)
    if action == 'pulse_burst':
        status = value.get('status', 'unknown')
        return (status, 0 if status in {'burst_completed', 'signal_candidate_for_review'} else 2)
    if action == 'pulse_and_assess':
        status = value.get('observation', {}).get('status', 'unknown')
        if status != 'completed':
            return status, 2
        decision = value.get('decision', 'insufficient_acceptance_evidence')
        return decision, 0 if decision == 'candidate_for_next_stage' else 2
    return 'completed', 0


def main(argv: list[str] | None = None) -> int:
    initial = argparse.ArgumentParser(add_help=False)
    initial.add_argument("--session-file", default=os.environ.get("SPIRE_CONTROL_SESSION"))
    preliminary, _ = initial.parse_known_args(argv)
    try:
        session = load_control_session(preliminary.session_file) if preliminary.session_file else {}
    except (ValueError, OSError) as error:
        initial.error(str(error))
    parser = argparse.ArgumentParser(description="Audited SPiRE live control commands.")
    parser.add_argument("--session-file", default=preliminary.session_file)
    parser.add_argument("--host", default=session.get("host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=session.get("port", 6502))
    parser.add_argument("--timeout-s", type=float, default=session.get("timeout_s", 3.0))
    parser.add_argument("--active-run-db", type=Path, default=session.get("active_run_db"),
        help="Durable action audit; required for non-protective control steps.")
    parser.add_argument("--authorization", type=Path, default=session.get("authorization"),
        help="Explicit local, expiring authorization; never changes default live permissions.")
    parser.add_argument("--stop-file", type=Path, default=session.get("stop_file"),
        help="Existing marker cancels bounded writes and a running Auto Approach; protective stops remain available.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("drift-status", help="Read XYZ drift settings and saturation only; no write authorization required.")
    start_scan = commands.add_parser(
        "start-scan",
        help="Start the existing Nanonis frame/speed/buffer/save configuration.",
    )
    direction = start_scan.add_mutually_exclusive_group(required=True)
    direction.add_argument("--up", action="store_true")
    direction.add_argument("--down", action="store_true")
    step = commands.add_parser("step", help="Execute exactly one audited request; no automatic retries.")
    request_source = step.add_mutually_exclusive_group()
    request_source.add_argument("--request-file", type=Path)
    request_source.add_argument("--action", help="Existing control_step action; does not add or unlock actions.")
    request_source.add_argument("--preset", help="Reuse a named request from --session-file.")
    step.add_argument("quick", nargs="*", help="pulse | poke x y [depth] | move x y | scan x y size [frame_time_s]; positions/sizes in nm")
    step.add_argument("--dry-run", action="store_true", help="Expand request only; no connection, audit or instrument writes.")
    step.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                      help="Repeat for each argument; numbers/booleans/JSON or bare enum text.")
    step.add_argument("--purpose", help="Required with --action; recorded in the existing audit.")
    step.add_argument("--result-file", type=Path, help="New result artifact; reserve before connecting, never overwrite/retry.")
    step.add_argument("--observer-port", type=int, default=session.get("observer_port"), help="Independent direct observation port for pulse_and_assess.")
    observe = commands.add_parser("observe-feedback", help="Finite direct feedback observation; no recorder data reads.")
    observe.add_argument("--duration-s", type=float, required=True)
    observe.add_argument("--interval-s", type=float, default=.02)
    observe.add_argument("--maximum-sample-gap-s", type=float, default=.5)
    observe.add_argument("--maximum-abs-current-a", type=float, required=True)
    observe.add_argument("--z-margin-m", type=float, required=True)
    observe.add_argument("--settling-s", type=float, default=0.)
    observe.add_argument("--overcurrent-hold-s", type=float, default=0.)
    observe.add_argument("--withdraw-on-overcurrent", type=int, choices=(0, 1), default=1)
    observe.add_argument("--result-file", type=Path, required=True)
    observe.add_argument("--progress-file", type=Path, required=True)
    observe.add_argument("--stop-scan-at-end", action="store_true")
    args = parser.parse_args(argv)

    if args.command not in {"observe-feedback", "drift-status"} and args.active_run_db is None:
        parser.error("Control steps require --active-run-db")
    request = None
    if args.command == "step":
        try:
            request = build_step_request(request_file=args.request_file, action=args.action,
                                         params=args.param, purpose=args.purpose, preset=args.preset,
                                         quick=args.quick, session=session)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        if args.dry_run:
            print(json.dumps({"request": request, "mode": "dry_run", "authorizes_live_action": False}, ensure_ascii=False, allow_nan=False))
            return 0
        if args.result_file is None and session.get("output_directory"):
            directory = Path(session["output_directory"])
            directory.mkdir(parents=True, exist_ok=True)
            args.result_file = directory / (uuid.uuid4().hex + ".json")
        if request.get("action") in {"pulse_and_assess", "pulse_burst", "acquire_bias_spectrum"} and (args.observer_port is None or args.observer_port == args.port):
            parser.error("This acquisition requires a distinct --observer-port")
    audit_error = None
    try:
        sink = ExistingRunActionEventSink(args.active_run_db) if args.active_run_db else None
    except Exception as error:
        if args.command != "observe-feedback":
            raise
        sink = None
        audit_error = str(error)
    config = NanonisConnectionConfig(
        host=args.host,
        port=args.port,
        timeout_s=args.timeout_s,
        backend="live",
    )
    gate = load_control_authorization(args.authorization, connection=config) if args.authorization else None
    if args.command == "step" and gate is None:
        parser.error("step requires an explicit --authorization file")
    if args.command == "observe-feedback":
        if args.result_file.exists() or args.progress_file.exists():
            parser.error("Observation output files must be new")
        def progress(value):
            temporary = args.progress_file.with_suffix(args.progress_file.suffix + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(args.progress_file)
        with args.result_file.open("x", encoding="utf-8") as stream:
            # Protective-only observer: never block a stop on database I/O.
            # Retain original typed action timestamps, then flush after stopping.
            protective_events = []
            with NanonisDevice(config=config, live=True, safety_gate=gate,
                               action_event_sink=protective_events.append,
                               stop_requested=args.stop_file.exists if args.stop_file else None) as observer:
                result = observe_feedback(observer, duration_s=args.duration_s, interval_s=args.interval_s,
                    maximum_sample_gap_s=args.maximum_sample_gap_s,
                    maximum_abs_current_a=args.maximum_abs_current_a, z_margin_m=args.z_margin_m,
                    settling_s=args.settling_s, overcurrent_hold_s=args.overcurrent_hold_s,
                    withdraw_on_overcurrent=args.withdraw_on_overcurrent,
                    stop_requested=args.stop_file.exists if args.stop_file else lambda: False,
                    progress=progress, stop_scan_at_end=args.stop_scan_at_end)
            result["protective_action_events"] = to_jsonable(protective_events)
            result["audit_error"] = audit_error
            if sink is not None:
                try:
                    for event in protective_events:
                        sink(event)
                except Exception as error:
                    result["audit_error"] = str(error)
            json.dump(to_jsonable(result), stream, ensure_ascii=False, allow_nan=False)
        print(json.dumps({key: value for key, value in result.items() if key != "rows"},
                         ensure_ascii=False, allow_nan=False))
        return 0 if result["status"] == "completed" else 2
    with ExitStack() as stack:
        output = stack.enter_context(args.result_file.open("x", encoding="utf-8")) if args.command == "step" and args.result_file else None
        if output is not None:
            json.dump({"request": request, "status": "pending"}, output, ensure_ascii=False, allow_nan=False)
            output.flush()
        try:
            with NanonisDevice(
                config=config,
                live=True,
                action_event_sink=sink,
                safety_gate=gate,
                stop_requested=args.stop_file.exists if args.stop_file else None,
            ) as device:
                if args.command == "drift-status":
                    print(json.dumps(to_jsonable(device.get_drift_compensation()), indent=2, ensure_ascii=False))
                    return 0
                if args.command == "start-scan":
                    request = {"action": "start_scan", "arguments": {"direction_up": bool(args.up)},
                               "purpose": "Start existing scan configuration"}
                elif args.command == "step":
                    pass  # Parsed and checked before opening any instrument connection.
                else:  # pragma: no cover - argparse prevents this branch
                    parser.error(f"unsupported command: {args.command}")
                if request["action"] in {"pulse_and_assess", "pulse_burst", "acquire_bias_spectrum"}:
                    if args.observer_port is None or args.observer_port == args.port:
                        parser.error("pulse_and_assess requires a distinct --observer-port")
                    events = []
                    observer_timeout = (request["arguments"]["observation_policy"]["maximum_sample_gap_s"]
                                        if request["action"] == "acquire_bias_spectrum" else args.timeout_s)
                    observer_config = NanonisConnectionConfig(host=args.host, port=args.observer_port,
                        timeout_s=min(args.timeout_s, observer_timeout), backend="live")
                    observer_failure = None
                    try:
                        with NanonisDevice(config=observer_config, live=True, action_event_sink=events.append,
                                           stop_requested=device.control_stop_requested) as observer:
                            result = execute_control_step(device, request, observer=observer)
                        result["protective_action_events"] = to_jsonable(events)
                    except BaseException as error:
                        observer_failure = error
                        error.acquired_evidence = (getattr(error, 'acquired_evidence', None) or {}) | {
                            "protective_action_events": to_jsonable(events)}
                        raise
                    finally:
                        # Protection already ran without database I/O. Retain its
                        # events on failure as well as success; never mask the cause.
                        for event in events:
                            try:
                                sink(event)
                            except Exception as audit_error:
                                if observer_failure is not None:
                                    observer_failure.acquired_evidence["observer_audit_error"] = str(audit_error)
                                else:
                                    result["observer_audit_error"] = str(audit_error)
                                break  # Full events remain in the result/error artifact.
                else:
                    result = execute_control_step(device, request)
        except BaseException as error:
            if output is not None:
                output.seek(0)
                output.truncate()
                json.dump({"request": request, "status": "failed", "error_type": type(error).__name__,
                           "error": str(error), "acquired_evidence": to_jsonable(getattr(error, 'acquired_evidence', None)),
                           "error_notes": list(getattr(error, '__notes__', ())),
                           "retry_policy": "Inspect typed audit and independently read state; never automatically retry."},
                          output, ensure_ascii=False, allow_nan=False)
            raise
        outcome, exit_code = step_outcome(request['action'], result)
        if output is not None:
            output.seek(0)
            output.truncate()
            json.dump({"request": request, "status": "completed" if exit_code == 0 else "needs_review",
                       "workflow_status": outcome, "result": to_jsonable(result)},
                      output, ensure_ascii=False, allow_nan=False)
    display = result.get("result") if args.command == "step" and session else result
    if request["action"] == "observe_noise":
        display = {key: value for key, value in result["result"].items() if key not in ("rows", "spectra")}
    if request["action"] == "pulse_burst":
        display = {key: value for key, value in result["result"].items() if key != "pulses"}
    if request["action"] == "pulse_and_assess":
        value = result["result"]
        display = {"decision": value["decision"], "status": value["observation"]["status"],
                   "response": value.get("response"), "result_file": str(args.result_file) if args.result_file else None}
    if args.command == "step" and session:
        display = {"request": request, "workflow_status": outcome, "result_file": str(args.result_file),
                   "result": display, "tip_quality": result.get("tip_quality")}
    print(json.dumps(to_jsonable(display), indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
