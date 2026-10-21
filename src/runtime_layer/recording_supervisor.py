"""Bounded process supervision of the existing recorder; no instrument commands."""
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from data_layer.recording import (MIN_RECORDING_FREE_BYTES, recording_root_free_bytes,
                                 recording_tree_size_bytes)

from .recording_control import DEFAULT_RECORDING_CONFIG, load_recording_settings


def service_directory(root):
    return Path(root) / ".recorder-service"


def write_health(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload | {"pid": os.getpid(), "utc_ns": time.time_ns(),
        "monotonic_s": time.monotonic()}, ensure_ascii=False) + "\n", encoding="utf-8")
    # Windows readers may briefly deny delete/rename sharing. Keep the previous
    # complete snapshot until replacement succeeds; never rewrite it in place.
    # This retries a small health file only, never an instrument command. The
    # bounded 35 ms wait remains visible to the recorder's cadence accounting.
    for attempt in range(4):
        try:
            temporary.replace(path)
            break
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 3:
                raise
            logging.warning("health_publish_retry path=%s attempt=%s winerror=%s",
                            path, attempt + 1, error.winerror)
            time.sleep((.005, .010, .020)[attempt])


def read_health(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@contextmanager
def recording_lease(root, name="recorder"):
    """OS-owned lock: a crash releases it; never trust or delete a stale PID file."""
    directory = service_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"Another {name} process owns recording root {root}") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def service_status(root):
    directory = service_directory(root)
    supervisor = read_health(directory / "supervisor.json")
    worker = read_health(directory / "worker.json")
    for value in (supervisor, worker):
        if value:
            value["heartbeat_age_s"] = max(0, (time.time_ns() - value["utc_ns"]) / 1e9)
            value["heartbeat_fresh"] = value["heartbeat_age_s"] < 30
    return {"supervisor": supervisor, "worker": worker,
            "stop_requested": (directory / "stop").exists()}


def request_service_stop(root):
    directory = service_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stop").touch()


def _finish_child(child, stop_file, logger, grace_s=120):
    if child.poll() is None:
        stop_file.touch()
        try:
            child.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            logger.error("worker_forced_stop pid=%s; run may be unfinalized; preserve all raw files", child.pid)
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)


def supervise_recording(*, live=False, segment_seconds=3600, maximum_runs=None,
                        config_path=DEFAULT_RECORDING_CONFIG, local_path=None):
    """Restart fresh workers with backoff; never restart hardware or remove data."""
    if not math.isfinite(segment_seconds) or segment_seconds <= 0:
        raise ValueError("segment_seconds must be finite and positive")
    def settings_loader():
        return load_recording_settings(config_path, local_path)
    settings = settings_loader()
    if settings.observe_gui_actions or settings.data_logger_enabled:
        raise ValueError("Unattended mode requires GUI hooks and Data Logger writes disabled")
    with recording_lease(settings.root, "supervisor"):
        return _supervise_owned(settings, settings_loader, live=live,
            segment_seconds=segment_seconds, maximum_runs=maximum_runs,
            config_path=config_path, local_path=local_path)


def _supervise_owned(settings, settings_loader, *, live, segment_seconds,
                     maximum_runs, config_path, local_path):
    directory = service_directory(settings.root)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"spire.recorder.{os.getpid()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(directory / "service.log", maxBytes=1024**2,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    child = None
    worker_stop = directory / "worker.stop"
    service_stop = directory / "stop"
    service_health = directory / "supervisor.json"
    worker_health = directory / "worker.json"
    state = "starting"
    failures = 0
    completed = 0
    previous_end_ns = None
    try:
        if service_stop.exists():
            state = "stopped"
            return 0
        logger.info("supervisor_started live=%s segment_seconds=%s root=%s", live, segment_seconds, settings.root)
        while maximum_runs is None or completed < maximum_runs:
            current = settings_loader()
            if current.root.resolve() != settings.root.resolve():
                raise RuntimeError("Recording root changed; stop and redeploy the service")
            if service_stop.exists() or not current.enabled:
                state = "stopped"
                break
            if current.observe_gui_actions or current.data_logger_enabled:
                raise ValueError("Unattended mode cannot enable GUI hooks or Data Logger writes")
            used = recording_tree_size_bytes(settings.root)
            free = recording_root_free_bytes(settings.root)
            if used >= current.storage_quota_bytes or free < MIN_RECORDING_FREE_BYTES:
                state = "storage_wait"
                logger.warning("storage_wait used_bytes=%s free_bytes=%s; raw data retained", used, free)
                for _ in range(12):
                    write_health(service_health, {"phase": state, "used_bytes": used, "free_bytes": free})
                    if service_stop.exists():
                        break
                    time.sleep(5)
                continue
            # Respect a foreground recorder (including an orphan from a
            # supervisor crash) before touching its stop/health files.
            try:
                with recording_lease(settings.root):
                    worker_stop.unlink(missing_ok=True)
            except RuntimeError:
                state = "waiting_for_existing_recorder"
                write_health(service_health, {"phase": state})
                time.sleep(5)
                continue
            executable = Path(sys.executable)
            if executable.name.lower() == "pythonw.exe":
                executable = executable.with_name("python.exe")
            command = [str(executable), "-B", "-m", "runtime_layer.recorder_cli",
                "--config", str(config_path)]
            if local_path is not None:
                command.extend(["--local-config", str(local_path)])
            command.extend(["record", "--duration-s", str(segment_seconds), "--defer-review",
                "--health-file", str(worker_health), "--stop-file", str(worker_stop)]
            )
            if live:
                command.append("--live")
            output = directory / "worker.log"
            if output.exists() and output.stat().st_size > 1024**2:
                output.replace(directory / "worker.previous.log")
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0
            with output.open("ab") as stream:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.STDOUT, creationflags=flags)
                started = time.monotonic()
                last_publish = 0.0
                next_settings_check = 0.0
                enabled = current.enabled
                ready = False
                worker_unhealthy = False
                logger.info("worker_started pid=%s; run boundaries are not gap-free", child.pid)
                while child.poll() is None:
                    now = time.monotonic()
                    health = read_health(worker_health)
                    own_health = health if health.get("pid") == child.pid else {}
                    if own_health.get("phase") == "recording" and not ready:
                        ready = True
                        logger.info("recording_ready pid=%s run=%s boundary_start_utc_ns=%s boundary_end_utc_ns=%s",
                            child.pid, own_health.get("run_id"), previous_end_ns, own_health["utc_ns"])
                    state = "recording" if ready else "connecting"
                    if now - last_publish >= 10:
                        write_health(service_health, {"phase": state, "worker_pid": child.pid,
                            "completed_runs": completed, "consecutive_failures": failures,
                            "segment_seconds": segment_seconds, "live": live})
                        last_publish = now
                    if now >= next_settings_check:
                        enabled = settings_loader().enabled
                        next_settings_check = now + 5
                    if service_stop.exists() or not enabled:
                        state = "stopped"
                        _finish_child(child, worker_stop, logger)
                        break
                    elapsed = now - started
                    age = now - own_health.get("monotonic_s", started)
                    deadline = 180 if own_health.get("phase") == "finalizing" else 60
                    # A missed acquisition deadline compromises the evidence,
                    # but the recorder deliberately continues collecting. Do not
                    # turn a recorded timing gap into a stop/retry recording gap.
                    unhealthy_components = {
                        name: error for name, error in (own_health.get("component_failures") or {}).items()
                        if name != "telemetry_cadence"
                    }
                    if (age > deadline or elapsed > segment_seconds + 240
                            or unhealthy_components or own_health.get("writer_failed")):
                        worker_unhealthy = True
                        logger.error("worker_unhealthy pid=%s heartbeat_age_s=%.1f details=%s", child.pid, age, own_health)
                        _finish_child(child, worker_stop, logger)
                        break
                    time.sleep(1)
                code = child.wait()
                final = read_health(worker_health)
                if final.get("pid") == child.pid:
                    previous_end_ns = final.get("acquisition_stopped_utc_ns", final.get("utc_ns"))
                logger.info("worker_finished pid=%s exit=%s health=%s", child.pid, code, final)
                # A sealed segment with only timing gaps keeps its quality
                # warning, but needs no failure backoff before the next segment.
                cadence_only_rotation = (
                    code == 3 and not worker_unhealthy
                    and final.get("pid") == child.pid
                    and final.get("phase") == "finished"
                    and final.get("status") == "recording_compromised"
                    and final.get("duration_completed") is True
                    and set(final.get("component_failures") or {}) == {"telemetry_cadence"}
                    and final.get("review_error") is None
                )
                child = None
            completed += 1
            if state == "stopped":
                break
            normal_rotation = code == 0 or cadence_only_rotation
            failures = 0 if normal_rotation else failures + 1
            if maximum_runs is not None and completed >= maximum_runs:
                break
            delay = 0 if normal_rotation else min(60, 5 * 2 ** min(failures - 1, 4))
            retry_at = time.monotonic() + delay
            while time.monotonic() < retry_at and not service_stop.exists():
                state = "retry_wait"
                write_health(service_health, {"phase": state, "last_exit_code": code,
                    "consecutive_failures": failures, "retry_in_s": round(retry_at - time.monotonic(), 1)})
                time.sleep(min(5, max(0, retry_at - time.monotonic())))
        state = "stopped"
    except KeyboardInterrupt:
        state = "stopped"
    except Exception:
        state = "failed"
        logger.exception("supervisor_failed")
        raise
    finally:
        if child is not None:
            _finish_child(child, worker_stop, logger)
        write_health(service_health, {"phase": state, "completed_runs": completed})
        logger.removeHandler(handler)
        handler.close()
    return 0
