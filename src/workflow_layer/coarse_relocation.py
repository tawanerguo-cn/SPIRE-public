"""Reusable retreat/relocate/approach transaction; current execution is fake-only.

This workflow never pulses, retries uncertain movement, or declares tip quality.
Instrument methods own bounds, direction encoding and command implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from data_layer import RunStore
from instrument_layer import CoarseXYDirection, FakeNanonisBackend, NanonisDevice, ZControllerStatus
from instrument_layer.models import to_jsonable


@dataclass(frozen=True)
class CoarseRelocationPlan:
    xy_direction: CoarseXYDirection
    retract_steps: int
    xy_steps: int
    group: int
    approach_timeout_ms: int = 300000
    approach_poll_interval_ms: int = 100

    def __post_init__(self):
        object.__setattr__(self, "xy_direction", CoarseXYDirection(self.xy_direction))


class CoarseRelocationWorkflow:
    """Run once with explicit parameters and durable evidence at each stage."""

    def __init__(self, device: NanonisDevice):
        self.device = device

    def validate(self, plan: CoarseRelocationPlan) -> None:
        # Validate the entire sequence before changing feedback or moving anything.
        checks = (
            self.device.validate_coarse_z_retract(plan.retract_steps, group=plan.group),
            self.device.validate_coarse_xy_move(plan.xy_direction, plan.xy_steps, group=plan.group),
            self.device.validate_control_action("z_controller_set", {"enabled": 0}),
            self.device.validate_control_action("auto_approach", {
                "timeout_ms": plan.approach_timeout_ms,
                "poll_interval_ms": plan.approach_poll_interval_ms,
            }),
        )
        for check in checks:
            check.raise_for_violations()

    def run(self, plan: CoarseRelocationPlan, *, run_store: RunStore) -> dict:
        # Even a caller-supplied enabled gate cannot commission this workflow.
        if not self.device.is_fake_backend:
            raise RuntimeError("coarse relocation is fake-only pending station commissioning")

        def event(kind, payload):
            run_store.append_event(kind, to_jsonable(payload),
                                   monotonic_ns=time.perf_counter_ns(), utc_ns=time.time_ns())

        previous_sink = self.device.action_events.set_sink(run_store.append_action_event)
        stage = "preflight"
        changed = False
        try:
            event("coarse_relocation_planned", {"plan": plan, "mode": "fake_only"})
            self.validate(plan)
            if (self.device.get_scan_status().running or self.device.get_bias_spectroscopy_status()
                    or self.device.get_auto_approach_running()):
                raise RuntimeError("relocation requires scan, spectroscopy and Auto Approach stopped")
            before = self.device.get_tip_state_snapshot()
            if before.z_controller_status not in {ZControllerStatus.ON, ZControllerStatus.OFF, ZControllerStatus.HOLD}:
                raise RuntimeError("relocation requires a known stable Z controller state")
            event("coarse_relocation_precheck", {"tip_state": before})

            stage = "feedback_off"
            # Mark uncertainty before the first write, including a lost acknowledgement.
            changed = True
            self.device.set_z_controller_on(False)
            stage = "coarse_z_retract"
            retreat = self.device.retract_coarse_z(plan.retract_steps, group=plan.group)
            event("coarse_relocation_retracted", {"result": retreat})
            stage = "coarse_xy_move"
            move = getattr(self.device, f"move_coarse_{plan.xy_direction.value}")
            relocated = move(plan.xy_steps, group=plan.group)
            event("coarse_relocation_relocated", {"result": relocated})
            stage = "auto_approach"
            approach = self.device.run_auto_approach(timeout_ms=plan.approach_timeout_ms,
                poll_interval_ms=plan.approach_poll_interval_ms)
            event("coarse_relocation_approached", {"result": approach})
            stage = "state_postcheck"
            after = self.device.get_tip_state_snapshot()
            if (not after.z_controller_on or after.z_controller_status is not ZControllerStatus.ON
                    or self.device.get_scan_status().running
                    or self.device.get_bias_spectroscopy_status() or self.device.get_auto_approach_running()):
                raise RuntimeError("unexpected process/feedback state after relocation")
            result = {
                "status": "awaiting_postcheck", "mode": "fake_only",
                "plan": to_jsonable(plan), "tip_state_after": to_jsonable(after),
                "authorizes_live_action": False, "tip_quality": "not_assessed",
                "physical_clearance_verified": False,
                "required_next_step": "Acquire and assess a fresh scan/spectrum; do not reuse a pre-relocation observation.",
                "evidence": "Synthetic command sequence and state checks, not physical displacement or repair success.",
            }
            event("coarse_relocation_awaiting_postcheck", result)
            return result
        except BaseException as error:
            stop_errors = []
            if changed:
                # Try every protective stop independently; one failed stop must not block the others.
                for stop in (self.device.stop_motor, self.device.stop_auto_approach,
                             self.device.stop_scan, self.device.stop_follow_me, self.device.stop_bias_spectroscopy):
                    try:
                        stop()
                    except Exception as stop_error:
                        stop_errors.append(f"{stop.__name__}: {stop_error}")
            try:
                event("coarse_relocation_failed", {
                    "stage": stage, "error_type": type(error).__name__, "error": str(error),
                    "stop_errors": stop_errors, "movement_may_have_occurred": changed,
                    "recovery": "No automatic retry, XY continuation, approach, feedback restoration or pulse.",
                })
            except Exception as audit_error:
                error.add_note(f"failure audit also failed: {audit_error}")
            raise
        finally:
            self.device.action_events.set_sink(previous_sink)


def rehearse_coarse_relocation(plan: CoarseRelocationPlan, output_root) -> dict:
    """Assembly owns a new fake device and RunStore; no live switch or connection setting."""
    store = RunStore.create(output_root, signal_catalog={}, requested_period_ns=0)
    device = NanonisDevice(backend=FakeNanonisBackend(scan_running=False))
    result = {"run_id": store.run_id, "run_directory": str(store.run_dir.resolve()),
              "database_path": str(store.database_path.resolve()),
              "mode": "fake_only", "authorizes_live_action": False}
    final_status = "failed"
    try:
        result |= CoarseRelocationWorkflow(device).run(plan, run_store=store)
        # RunStore terminal status describes recording completion, not tip quality.
        final_status = "complete"
    except Exception as error:
        result |= {"status": "failed", "error_type": type(error).__name__, "error": str(error),
                   "recovery": "Inspect the persisted run. No automatic resume or repeated movement."}
    finally:
        try:
            store.finalize(final_status, stopped_utc_ns=time.time_ns())
        finally:
            store.close()
            device.close()
    return result
