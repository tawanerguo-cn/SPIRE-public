"""Finite read-only lock-in diagnosis through the existing typed device.

Inherited settings are not a calibrated dI/dV measurement or permission to act.
No retries, settings writes, start/stop or automatic cleanup.
"""
import time

from instrument_layer import NanonisCommandError
from instrument_layer.models import to_jsonable


def inspect_lock_in_configuration(device, *, modulator_number=1, demodulator_number=1,
                                  phase_registers=()):
    for number in (modulator_number, demodulator_number):
        if type(number) is not int or number < 1:
            raise ValueError("Lock-in numbers must be positive integers")
    if (not isinstance(phase_registers, (list, tuple))
            or any(type(r) is not int or not 1 <= r <= 8 for r in phase_registers)
            or len(set(phase_registers)) != len(phase_registers)):
        raise ValueError("Explicit phase registers must be unique integers from 1 to 8")
    report = dict(started_utc_ns=time.time_ns(), status="reading", readings={}, errors={},
                  authorizes_next_action=False, scientific_acceptance="not_assessed",
                  limitations=["Sequential readbacks are not an atomic configuration snapshot.",
                      "Modulator OFF outside a spectrum does not establish its state during Lock-in Run.",
                      "Explicit frequency reads do not resolve an unknown modulator/demodulator binding.",
                      "Amplitude units follow the selected signal; RMS/peak convention and gain are uncalibrated.",
                      "Filter readback alone does not validate spectroscopy settling/integration time."])
    reads = [("modulator", lambda: device.get_lock_in_modulator(modulator_number)),
             ("demodulator", lambda: device.get_lock_in_demodulator(demodulator_number)),
             ("spectroscopy", device.get_bias_spectroscopy_advanced_settings),
             ("spectroscopy_timing", device.get_bias_spectroscopy_timing)]
    reads += [(f"phase_register_{r}", lambda r=r: device.get_lock_in_phase_frequency_hz(r))
              for r in phase_registers]
    for name, read in reads:
        try:
            report["readings"][name] = to_jsonable(read())
        except Exception as error:
            report["errors"][name] = dict(error_type=type(error).__name__, error=str(error),
                acquired_evidence=to_jsonable(getattr(error, "acquired_evidence", None)))
            # Only completed command/validation errors permit independent reads.
            if not isinstance(error, NanonisCommandError):
                report["status"] = "failed"
                break
    else:
        report["status"] = "partial" if report["errors"] else "completed"
    report["finished_utc_ns"] = time.time_ns()
    return report
