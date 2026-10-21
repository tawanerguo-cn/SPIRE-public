"""One spectrum with the existing finite observer active on an independent port."""
from __future__ import annotations

import math
from threading import Event, Thread

from instrument_layer import InstrumentPreconditionError, InstrumentPostconditionError
from instrument_layer.models import BiasSpectroscopyRecipe, to_jsonable
from .feedback_observation import observe_feedback, validate_feedback_observation_policy


def acquire_guarded_spectrum(device, observer, *, observation_policy, preparation, **recipe_values):
    recipe = BiasSpectroscopyRecipe.from_mapping(recipe_values)
    policy = validate_feedback_observation_policy(observer, observation_policy,
                                                  observation_mode="spectroscopy")
    device.validate_bias_spectroscopy_recipe(recipe).raise_for_violations()
    if (device is observer or device.config.host != observer.config.host
            or device.config.port == observer.config.port):
        raise ValueError("Spectroscopy requires a distinct observer connection to the same host")
    if observer.config.timeout_s > policy["maximum_sample_gap_s"]:
        raise ValueError("Observer socket timeout must not exceed the permitted observation gap")
    if policy["duration_s"] <= recipe.estimated_duration_s + recipe.restoration_timeout_ms / 1000:
        raise ValueError("Guard watchdog must cover acquisition and restoration confirmation")
    if set(preparation) != {"bias_v", "setpoint_a"} or not all(
            math.isfinite(float(v)) for v in preparation.values()):
        raise ValueError("Explicit finite preparation bias and setpoint are required")
    for key, actual in (("bias_v", observer.get_bias_v()), ("setpoint_a", observer.get_z_setpoint_a())):
        if not math.isclose(actual, preparation[key], rel_tol=1e-6, abs_tol=abs(preparation[key])*1e-6):
            raise InstrumentPreconditionError(f"Spectroscopy preparation {key} readback mismatch")
    if (not observer.get_z_controller_on() or observer.get_scan_status().running
            or observer.get_auto_approach_running() or observer.get_bias_spectroscopy_status()):
        raise InstrumentPreconditionError("Spectroscopy guard requires initially idle, engaged feedback")

    ready, finished = Event(), Event()
    state = {"report": None, "latest": None}

    def progress(value):
        state["latest"] = value
        if value["status"] == "observing" and abs(value["current_a"]) <= policy["maximum_abs_current_a"]:
            ready.set()

    def observe():
        try:
            state["report"] = observe_feedback(observer, **policy,
                observation_mode="spectroscopy", finished=finished.is_set,
                stop_requested=device.control_stop_requested, progress=progress)
        finally:
            ready.set()

    worker = Thread(target=observe, name="finite-spectroscopy-current-guard")
    worker.start()
    ready.wait()

    def active():
        return worker.is_alive() and state["latest"] is not None and state["latest"]["status"] == "observing"

    acquisition, error = None, None
    try:
        if not active():
            raise InstrumentPreconditionError("Current guard did not become ready; spectroscopy not started")
        acquisition = device.acquire_bias_spectrum_transaction(recipe, observation_ready=active)
    except BaseException as caught:
        error = caught
    finally:
        finished.set()
        worker.join()
    report = state["report"]
    if error is None and (report is None or report["status"] != "completed"):
        error = InstrumentPostconditionError("Spectroscopy current guard aborted; spectrum is not accepted")
    if error is not None:
        evidence = getattr(error, "acquired_evidence", {})
        error.acquired_evidence = evidence | {"current_guard": report,
            "acquisition": to_jsonable(acquisition), "preparation": preparation}
        raise error
    return {"acquisition": acquisition, "current_guard": report,
            "preparation": preparation, "tip_quality": "awaiting_spectrum_review"}
