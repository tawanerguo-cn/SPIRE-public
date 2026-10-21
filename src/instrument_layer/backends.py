from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect
from pathlib import Path
import socket
import struct
from threading import Condition
from typing import Any, Protocol

from .config import NanonisConnectionConfig


_LOCK_IN_READ_TYPES = {
    "LockIn_ModOnOffGet": ("I",), "LockIn_ModSignalGet": ("i",),
    "LockIn_ModAmpGet": ("f",), "LockIn_ModPhasRegGet": ("i",),
    "LockIn_ModPhasFreqGet": ("d",), "LockIn_ModHarmonicGet": ("i",),
    "LockIn_ModPhasGet": ("f",), "LockIn_DemodSignalGet": ("i",),
    "LockIn_DemodHarmonicGet": ("i",), "LockIn_DemodPhasRegGet": ("i",),
    "LockIn_DemodPhasGet": ("f",), "LockIn_DemodHPFilterGet": ("i", "f"),
    "LockIn_DemodLPFilterGet": ("i", "f"), "LockIn_DemodSyncFilterGet": ("I",),
    "LockIn_DemodRTSignalsGet": ("I",),
}


class _PriorityCommandGate:
    """Serialize one TCP stream while giving due telemetry reads precedence."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._active = False
        self._high_priority_waiters = 0

    @contextmanager
    def acquire(self, *, high_priority: bool):
        with self._condition:
            if high_priority:
                self._high_priority_waiters += 1
            try:
                while self._active or (
                    not high_priority
                    and self._high_priority_waiters > 0
                ):
                    self._condition.wait()
            finally:
                if high_priority:
                    self._high_priority_waiters -= 1
            self._active = True
        try:
            yield
        finally:
            with self._condition:
                self._active = False
                self._condition.notify_all()


class NanonisBackend(Protocol):
    """The official command surface used by :class:`NanonisDevice`."""

    is_live: bool

    def close(self) -> None: ...
    def Util_VersionGet(self) -> tuple[Any, ...]: ...
    def Util_AcqPeriodGet(self) -> tuple[Any, ...]: ...
    def Util_SessionPathGet(self) -> tuple[Any, ...]: ...
    def Bias_Get(self) -> tuple[Any, ...]: ...
    def Bias_Set(self, Bias_value_V: float) -> tuple[Any, ...]: ...
    def Bias_Pulse(self, *args: Any) -> tuple[Any, ...]: ...
    def Current_Get(self) -> tuple[Any, ...]: ...
    def ZCtrl_ZPosGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_ZPosSet(self, Z_position_m: float) -> tuple[Any, ...]: ...
    def ZCtrl_StatusGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_OnOffGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_OnOffSet(self, Z_controller_status: int) -> tuple[Any, ...]: ...
    def ZCtrl_HomePropsGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_Home(self) -> tuple[Any, ...]: ...
    def ZCtrl_LimitsEnabledGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_LimitsGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_TipLiftGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_SetpntGet(self) -> tuple[Any, ...]: ...
    def ZCtrl_SetpntSet(self, Setpoint_A: float) -> tuple[Any, ...]: ...
    def ZCtrl_Withdraw(self, Wait_until_finished: int, Timeout_ms: int) -> tuple[Any, ...]: ...
    def Scan_StatusGet(self) -> tuple[Any, ...]: ...
    def Scan_FrameGet(self) -> tuple[Any, ...]: ...
    def Scan_FrameSet(
        self,
        Center_X_m: float,
        Center_Y_m: float,
        Width_m: float,
        Height_m: float,
        Angle_deg: float,
    ) -> tuple[Any, ...]: ...
    def Scan_BufferGet(self) -> tuple[Any, ...]: ...
    def Scan_SpeedGet(self) -> tuple[Any, ...]: ...
    def Scan_SpeedSet(self, *args: Any) -> tuple[Any, ...]: ...
    def Piezo_RangeGet(self) -> tuple[Any, ...]: ...
    def Piezo_DriftCompGet(self) -> tuple[Any, ...]: ...
    def Piezo_DriftCompSet(self, Compensation_on_off: int, Vx_m_s: float,
                          Vy_m_s: float, Vz_m_s: float, Sat_Lim: float | None) -> tuple[Any, ...]: ...
    def LockIn_ModOnOffGet(self, Modulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_ModSignalGet(self, Modulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_ModAmpGet(self, Modulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_ModPhasRegGet(self, Modulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_ModPhasFreqGet(self, Phase_register: int) -> tuple[Any, ...]: ...
    def LockIn_ModHarmonicGet(self, Modulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_ModPhasGet(self, Modulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodSignalGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodHarmonicGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodPhasRegGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodPhasGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodHPFilterGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodLPFilterGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodSyncFilterGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def LockIn_DemodRTSignalsGet(self, Demodulator_number: int) -> tuple[Any, ...]: ...
    def Scan_XYPosGet(self, Wait_newest_data: int) -> tuple[Any, ...]: ...
    def Scan_FrameDataGrab(self, Channel_index: int, Data_direction: int) -> tuple[Any, ...]: ...
    def Scan_Action(self, Scan_action: int, Scan_direction: int) -> tuple[Any, ...]: ...
    def Scan_WaitEndOfScan(self, Timeout_ms: int) -> tuple[Any, ...]: ...
    def Signals_NamesGet(self) -> tuple[Any, ...]: ...
    def Signals_ValsGet(self, Signals_indexes: Sequence[int], Wait_for_newest_data: int) -> tuple[Any, ...]: ...
    def DataLog_ChsGet(self) -> tuple[Any, ...]: ...
    def DataLog_ChsSet(self, Channel_indexes: Sequence[int]) -> tuple[Any, ...]: ...
    def DataLog_PropsGet(self) -> tuple[Any, ...]: ...
    def DataLog_PropsSet(
        self,
        Acquisition_mode: int,
        Acquisition_duration_hours: int,
        Acquisition_duration_minutes: int,
        Acquisition_duration_seconds: float,
        Averaging: int,
        Basename: str,
        Comment: str,
        List_of_modules: Sequence[str],
    ) -> tuple[Any, ...]: ...
    def DataLog_StatusGet(self) -> tuple[Any, ...]: ...
    def DataLog_Start(self) -> tuple[Any, ...]: ...
    def DataLog_Stop(self) -> tuple[Any, ...]: ...
    def AutoApproach_OnOffGet(self) -> tuple[Any, ...]: ...
    def AutoApproach_OnOffSet(self, OnOff_status: int) -> tuple[Any, ...]: ...
    def SpectrumAnlzr_ChGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]: ...
    def SpectrumAnlzr_FreqRangeGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]: ...
    def SpectrumAnlzr_FreqResGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]: ...
    def SpectrumAnlzr_FFTWindowGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]: ...
    def SpectrumAnlzr_AveragGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]: ...
    def SpectrumAnlzr_ACCouplingGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]: ...
    def SpectrumAnlzr_DataGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]: ...
    def FolMe_XYPosGet(self, Wait_for_newest_data: int) -> tuple[Any, ...]: ...
    def FolMe_XYPosSet(self, X_m: float, Y_m: float, Wait_end_of_move: int) -> tuple[Any, ...]: ...
    def FolMe_SpeedGet(self) -> tuple[Any, ...]: ...
    def FolMe_Stop(self) -> tuple[Any, ...]: ...
    def BiasSpectr_StatusGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_AdvPropsGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_AdvPropsSet(self, Reset_Bias: int, Z_Controller_Hold: int,
                             Record_final_Z: int, Lockin_Run: int) -> tuple[Any, ...]: ...
    def BiasSpectr_MLSModeGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_MLSLockinPerSegGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_PropsGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_PropsSet(self, *args: Any) -> tuple[Any, ...]: ...
    def BiasSpectr_ChsGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_ChsSet(self, Channel_indexes: list[int]) -> tuple[Any, ...]: ...
    def BiasSpectr_LimitsGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_LimitsSet(self, Start_value_V: float, End_value_V: float) -> tuple[Any, ...]: ...
    def BiasSpectr_TimingGet(self) -> tuple[Any, ...]: ...
    def BiasSpectr_TimingSet(self, *args: float) -> tuple[Any, ...]: ...
    def BiasSpectr_Start(self, Get_data: int, Save_base_name: str) -> tuple[Any, ...]: ...
    def BiasSpectr_Stop(self) -> tuple[Any, ...]: ...
    def TipShaper_PropsGet(self) -> tuple[Any, ...]: ...
    def TipShaper_PropsSet(self, *args: Any) -> tuple[Any, ...]: ...
    def TipShaper_Start(self, Wait_until_finished: int, Timeout_ms: int) -> tuple[Any, ...]: ...
    def AtomTrack_StatusGet(self, AT_control: int) -> tuple[Any, ...]: ...
    def AtomTrack_PropsGet(self) -> tuple[Any, ...]: ...
    def AtomTrack_CtrlSet(self, AT_control: int, Status: int) -> tuple[Any, ...]: ...
    def Motor_StopMove(self) -> tuple[Any, ...]: ...
    def Motor_PosGet(self, Group: int, Timeout: int) -> tuple[Any, ...]: ...
    def Motor_StartMove(
        self,
        Direction: int,
        Number_of_steps: int,
        Group: int,
        Wait_until_finished: int,
    ) -> tuple[Any, ...]: ...


class VersionQueryUnavailableError(RuntimeError):
    """The server returned an error-only response to the optional version query."""


class OfficialNanonisBackend:
    """Live backend and the only class allowed to touch nanonis_spm or a TCP socket."""

    is_live = True

    @staticmethod
    def discover_client_methods() -> list[str]:
        """Inspect the installed official client without leaking its import upstream."""

        try:
            from nanonis_spm import Nanonis
        except ImportError as exc:
            raise RuntimeError(
                "Install the 'nanonis-spm' package to inspect live capabilities"
            ) from exc
        return sorted(
            name
            for name, value in inspect.getmembers(Nanonis)
            if callable(value) and not name.startswith("_")
        )

    def __init__(self, config: NanonisConnectionConfig, *, connection=None) -> None:
        self.config = config
        if connection is None:
            connection = socket.create_connection((config.host, config.port), timeout=config.timeout_s)
            connection.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )
            connection.settimeout(config.timeout_s)
        try:
            from nanonis_spm import Nanonis
        except ImportError as exc:
            raise RuntimeError("Install the 'nanonis-spm' package to use the live backend") from exc
        self._client = Nanonis(connection)
        self._command_gate = _PriorityCommandGate()

    def close(self) -> None:
        with self._command_gate.acquire(high_priority=False):
            self._client.close()

    def __enter__(self) -> "OfficialNanonisBackend":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def SpectrumAnlzr_DataGet(self, Spectrum_Analyzer_instance):
        """Decode protocol float64 axes/density; nanonis-spm 1.0.9 uses float32."""
        with self._command_gate.acquire(high_priority=False):
            response = self._client.send("SpectrumAnlzr.DataGet", [Spectrum_Analyzer_instance], ["i"])
            error = self._optional_response_error(response)
            if error is not None:
                return error, response, []
            return tuple(self._client.parseGeneralResponse(response, ["d", "d", "i", "*d"]))

    def BiasSpectr_StatusGet(self):
        """Repair nanonis-spm 1.0.9's missing uint32 response declaration."""

        with self._command_gate.acquire(high_priority=False):
            return self._client.quickSend(
                "BiasSpectr.StatusGet",
                [],
                [],
                ["I"],
            )

    def Piezo_DriftCompGet(self):
        """Decode seven/eight fields without consuming the error trailer as data."""
        with self._command_gate.acquire(high_priority=False):
            response = self._client.send("Piezo.DriftCompGet", [], [])
            error = self._optional_response_error(response)
            if error is not None:
                return error, response, []
            layouts = []
            for size, fields in ((28, ["I", "f", "f", "f", "I", "I", "I"]),
                                 (32, ["I", "f", "f", "f", "I", "I", "I", "f"])):
                if len(response) < size + 8:
                    continue
                status, error_size = struct.unpack_from(">Ii", response, size)
                if status in (0, 1) and error_size >= 0 and len(response) == size + 8 + error_size:
                    layouts.append((size, fields, status))
            if len(layouts) != 1:
                raise ValueError(f"Piezo.DriftCompGet unknown response layout; raw_body_hex={bytes(response).hex()}")
            size, fields, status = layouts[0]
            if status:
                error = bytes(response[size + 8:]).decode("utf-8", errors="replace") or f"Nanonis command error {status}"
                return error, response, []
            return tuple(self._client.parseGeneralResponse(response, fields))

    def Piezo_DriftCompSet(self, Compensation_on_off, Vx_m_s, Vy_m_s, Vz_m_s, Sat_Lim):
        with self._command_gate.acquire(high_priority=False):
            if Sat_Lim is None:
                # Four-argument protocol uses an on/off boolean. Only OFF/zero
                # speeds are exposed until legacy enable is commissioned.
                if (Compensation_on_off, Vx_m_s, Vy_m_s, Vz_m_s) != (2, 0., 0., 0.):
                    raise ValueError("Seven-field drift protocol supports only OFF/zero-speed requests")
                return self._client.quickSend("Piezo.DriftCompSet", [0, 0., 0., 0.], ["I", "f", "f", "f"], [])
            return self._client.Piezo_DriftCompSet(Compensation_on_off, Vx_m_s, Vy_m_s, Vz_m_s, Sat_Lim)

    def BiasSpectr_ChsGet(self):
        """Decode both documented and observed count-omitted channel-name arrays.

        Device 1's captured body omits the names count after the byte size.
        Normalize only an exactly bounded, fully valid string block/error trailer.
        No additional command or settings change is used to recover decoding.
        """
        with self._command_gate.acquire(high_priority=False):
            response = self._client.send("BiasSpectr.ChsGet", [], [])
            error = self._optional_response_error(response)
            if error is not None:
                return error, response, []
            try:
                normalized = self._normalize_spectroscopy_channel_names(response)
                parsed = list(self._client.parseGeneralResponse(normalized, ["i", "*i", "i", "i", "*+c"]))
                parsed[1] = response  # Preserve the actual wire body for evidence.
                return tuple(parsed)
            except (IndexError, ValueError, struct.error) as error:
                raise ValueError(f"BiasSpectr.ChsGet decode failed; raw_body_hex={bytes(response).hex()}") from error

    @staticmethod
    def _normalize_spectroscopy_channel_names(response):
        if len(response) < 16:
            return response
        count = struct.unpack_from(">i", response, 0)[0]
        if not 0 <= count <= 128:
            return response
        size_position = 4 + 4 * count
        if size_position + 12 > len(response):
            return response
        size = struct.unpack_from(">i", response, size_position)[0]
        begin, end = size_position + 4, size_position + 4 + size
        if size < 0 or end + 8 > len(response):
            return response
        status, error_size = struct.unpack_from(">Ii", response, end)
        if status not in (0, 1) or error_size < 0 or end + 8 + error_size != len(response):
            return response
        cursor, names_count = begin, 0
        while cursor < end:
            if cursor + 4 > end:
                return response
            length = struct.unpack_from(">i", response, cursor)[0]
            cursor += 4
            if length < 1 or cursor + length > end:
                return response
            if b"\x00" in response[cursor:cursor + length]:
                return response
            cursor += length
            names_count += 1
        if cursor != end or (count > 0 and names_count == 0):
            return response
        return response[:begin] + struct.pack(">i", names_count) + response[begin:]

    def BiasSpectr_PropsGet(self):
        """Use the current protocol's parameter lists and save flags (not channels)."""
        with self._command_gate.acquire(high_priority=False):
            response = self._client.send("BiasSpectr.PropsGet", [], [])
            error = self._optional_response_error(response)
            if error is not None:
                return error, response, []
            return tuple(self._client.parseGeneralResponse(
                response, ["H", "i", "H", "i", "i", "i", "*+c", "i", "i", "*+c", "H", "H"]
            ))

    def Util_VersionGet(self):
        """Repair nanonis-spm 1.0.9's send-only string response notation."""

        with self._command_gate.acquire(high_priority=False):
            response = self._client.send("Util.VersionGet", [], [])
            # Some servers reject this optional command with only the protocol
            # error trailer, without any of the expected version fields.
            error = self._optional_response_error(response)
            if error is not None:
                raise VersionQueryUnavailableError(error)
            return tuple(self._client.parseGeneralResponse(
                response, ["i", "*-c", "i", "*-c", "I", "I"]
            ))

    @staticmethod
    def _optional_response_error(response):
        """Recognize an entire error-only body, not an ordinary data prefix."""
        if len(response) >= 8:
            status, size = struct.unpack(">Ii", response[:8])
            if status and size >= 0 and len(response) == 8 + size:
                return bytes(response[8:]).decode("utf-8", errors="replace") or f"Nanonis command error {status}"
        return None

    def TipShaper_PropsGet(self):
        """Keep optional property rejection out of the client's data parser."""
        with self._command_gate.acquire(high_priority=False):
            response = self._client.send("TipShaper.PropsGet", [], [])
            error = self._optional_response_error(response)
            if error is not None:
                # Existing typed response handling raises NanonisCommandError.
                return error, response, []
            return tuple(self._client.parseGeneralResponse(
                response, ["f", "I", "f", "f", "f", "f", "f", "f", "f", "f", "I"]
            ))

    def __getattr__(self, name: str):
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def synchronized(*args, **kwargs):
            with self._command_gate.acquire(
                high_priority=name == "Signals_ValsGet"
            ):
                if name in _LOCK_IN_READ_TYPES:
                    # All typed callers use one positional index. Keep protocol
                    # error-only bodies out of nanonis-spm's data-first parser.
                    if len(args) != 1 or kwargs:
                        raise TypeError(f"{name} requires one positional index")
                    response = self._client.send(name.replace("_", ".", 1), list(args), ["i"])
                    error = self._optional_response_error(response)
                    if error is not None:
                        return error, response, []
                    fields = _LOCK_IN_READ_TYPES[name]
                    size = struct.calcsize(">" + "".join(fields))
                    if len(response) >= size + 8:
                        status, length = struct.unpack_from(">Ii", response, size)
                        if status in (0, 1) and length >= 0 and len(response) == size + 8 + length:
                            return tuple(self._client.parseGeneralResponse(response, list(fields)))
                    raise ValueError(f"{name} unknown response layout; raw_body_hex={bytes(response).hex()}")
                return attribute(*args, **kwargs)

        return synchronized


@dataclass
class FakeNanonisBackend:
    """Deterministic stateful backend for offline workflows and safety tests."""

    is_live = False

    version: tuple[Any, ...] = (28, "Nanonis SPM Control Software", 9, "Generic 5", 10771, 10771)
    session_path: str = r"C:\NanonisFakeSession"
    bias_v: float = -3.5
    current_a: float = -4.8112e-11
    z_position_m: float = 128e-9
    z_status_code: int = 2
    z_controller_on: bool = True
    z_limits_enabled: bool = True
    z_high_limit_m: float = 200e-9
    z_low_limit_m: float = -200e-9
    tip_lift_m: float = 2e-9
    home_relative: bool = False
    home_position_m: float = 0.0
    z_setpoint_a: float = 1e-12
    scan_running: bool = True
    scan_paused: bool = False
    scan_direction_up: bool = True
    scan_frame: tuple[float, float, float, float, float] = (-4.8174e-7, -4.4261e-7, 1e-7, 1e-7, 0.0)
    scan_buffer: tuple[int, list[int], int, int] = field(default_factory=lambda: (3, [0, 1, 2], 256, 256))
    scan_speed: tuple[float, float, float, float, int, float] = (100e-9, 200e-9, 1.0, 0.5, 0, 2.0)
    piezo_range: tuple[float, float, float] = (2e-6, 2e-6, 400e-9)
    drift_compensation: tuple[int, float, float, float, int, int, int, float] = (
        0, 0., 0., 0., 0, 0, 0, 10.)
    lock_in_enabled: bool = False
    lock_in_signal_index: int = 3
    lock_in_amplitude: float = .01
    lock_in_phase_register: int = 1
    lock_in_phase_frequencies: dict[int, float] = field(default_factory=lambda: {1: 421., 2: 823.})
    lock_in_harmonic: int = 1
    lock_in_phase_deg: float = 0.
    lock_in_demod_signal_index: int = 0
    lock_in_demod_harmonic: int = 1
    lock_in_demod_phase_register: int = 1
    lock_in_demod_phase_deg: float = 0.
    lock_in_demod_high_pass: tuple[int, float] = (0, 0.)
    lock_in_demod_low_pass: tuple[int, float] = (4, 100.)
    lock_in_demod_sync_filter: int = 0
    lock_in_demod_rt_signals: int = 0
    scan_xy_position: tuple[float, float] = (-4.8174e-7, -4.4261e-7)
    follow_me_position: tuple[float, float] = (-4.8174e-7, -4.4261e-7)
    follow_me_speed: tuple[float, bool] = (50e-9, True)
    frame_data: list[list[float]] = field(default_factory=lambda: [[0.0, 1.0], [1.0, 2.0]])
    signal_names: list[str] = field(
        default_factory=lambda: ["Current (A)", "Z (m)", "Input 2 (V)", "Bias (V)"]
    )
    signal_values: dict[int, float] = field(default_factory=lambda: {0: -4.8e-11, 1: 128e-9, 3: -3.5})
    spectrum_channel_indexes: dict[int, int] = field(default_factory=lambda: {1: 0, 2: 1})
    spectrum_frequency_ranges: dict[int, tuple[int, list[float]]] = field(
        default_factory=lambda: {1: (1, [1_000.0, 2_000.0, 4_000.0]), 2: (0, [500.0, 1_000.0])}
    )
    spectrum_frequency_resolutions: dict[int, tuple[int, list[float]]] = field(
        default_factory=lambda: {1: (0, [1.0, 2.0, 4.0]), 2: (1, [0.5, 1.0])}
    )
    spectrum_fft_windows: dict[int, int] = field(default_factory=lambda: {1: 1, 2: 0})
    spectrum_averaging: dict[int, tuple[int, int, int]] = field(
        default_factory=lambda: {1: (1, 0, 10), 2: (0, 0, 1)}
    )
    spectrum_ac_coupling: dict[int, bool] = field(default_factory=lambda: {1: False, 2: True})
    spectrum_data: dict[int, tuple[float, float, list[float]]] = field(
        default_factory=lambda: {1: (100.0, 1.0, [0.1, 0.2, 0.3]), 2: (200.0, 2.0, [0.4, 0.5])}
    )
    bias_spectroscopy_running: bool = False
    bias_spectroscopy_advanced: tuple[int, int, int, int] = (1, 1, 0, 1)
    bias_spectroscopy_sweep_mode: str = "Linear"
    bias_spectroscopy_lock_in_per_segment: int = 0
    bias_spectroscopy_save_all: bool = False
    bias_spectroscopy_sweeps: int = 1
    bias_spectroscopy_backward: bool = False
    bias_spectroscopy_points: int = 5
    bias_spectroscopy_autosave: bool = False
    bias_spectroscopy_show_save_dialog: bool = False
    bias_spectroscopy_limits: tuple[float, float] = (-0.1, 0.1)
    bias_spectroscopy_channel_indexes: list[int] = field(default_factory=lambda: [0, 1])
    bias_spectroscopy_channel_names: list[str] = field(default_factory=lambda: ["Bias (V)", "Current (A)"])
    bias_spectroscopy_parameter_names: list[str] = field(default_factory=lambda: ["X (m)", "Y (m)"])
    bias_spectroscopy_fixed_parameter_names: list[str] = field(default_factory=lambda: ["Bias (V)"])
    bias_spectroscopy_timing: tuple[float, float, float, float, float, float, float, float] = (
        0.01,
        0.0,
        0.01,
        10.0,
        0.001,
        0.002,
        0.01,
        0.01,
    )
    tip_shaper_properties: tuple[float, int, float, float, float, float, float, float, float, float, int] = (
        0.1,
        0,
        0.0,
        0.5e-9,
        0.1,
        0.0,
        0.1,
        -0.5e-9,
        0.1,
        0.1,
        1,
    )
    atom_tracking_status: dict[int, bool] = field(default_factory=lambda: {0: False, 1: False, 2: False})
    atom_tracking_properties: tuple[float, float, float, float, float] = (1.0, 1000.0, 10e-12, 0.0, 0.1)
    data_log_channel_indexes: list[int] = field(default_factory=lambda: [0, 1])
    data_log_timed: bool = False
    data_log_duration_s: float = 0.0
    data_log_averaging: int = 1
    data_log_basename: str = "SPiRE"
    data_log_comment: str = ""
    data_log_modules: list[str] = field(default_factory=list)
    data_log_running: bool = False
    data_log_start_time: str = ""
    data_log_stop_time: str = ""
    data_log_saved_file_path: str = ""
    data_log_saved_points: int = 0
    data_log_elapsed_s: float = 0.0
    auto_approach_running: bool = False
    auto_approach_complete_on_start: bool = True
    motor_positions: list[list[float]] = field(
        default_factory=lambda: [[0.0, 0.0, 0.0] for _ in range(6)]
    )
    motor_step_counts: list[list[int]] = field(
        default_factory=lambda: [[0, 0, 0] for _ in range(6)]
    )
    tip_shaper_bias_trace: list[float] = field(default_factory=list)
    command_log: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def close(self) -> None:
        self.command_log.append(("close", ()))

    def _record(self, name: str, *args: Any) -> None:
        self.command_log.append((name, args))

    def Util_VersionGet(self) -> tuple[Any, ...]:
        return self.version

    def Util_AcqPeriodGet(self) -> tuple[Any, ...]:
        return (0.02,)

    def Util_SessionPathGet(self) -> tuple[Any, ...]:
        return (len(self.session_path), self.session_path)

    def Bias_Get(self) -> tuple[Any, ...]:
        return (self.bias_v,)

    def Bias_Set(self, Bias_value_V: float) -> tuple[Any, ...]:
        self._record("Bias_Set", Bias_value_V)
        self.bias_v = float(Bias_value_V)
        return ()

    def Bias_Pulse(self, *args: Any) -> tuple[Any, ...]:
        self._record("Bias_Pulse", *args)
        return ()

    def Current_Get(self) -> tuple[Any, ...]:
        return (self.current_a,)

    def ZCtrl_ZPosGet(self) -> tuple[Any, ...]:
        return (self.z_position_m,)

    def ZCtrl_ZPosSet(self, Z_position_m: float) -> tuple[Any, ...]:
        self._record("ZCtrl_ZPosSet", Z_position_m)
        self.z_position_m = float(Z_position_m)
        self.signal_values[1] = float(Z_position_m)
        return ()

    def ZCtrl_StatusGet(self) -> tuple[Any, ...]:
        return (self.z_status_code,)

    def ZCtrl_OnOffGet(self) -> tuple[Any, ...]:
        self._record("ZCtrl_OnOffGet")
        return (1 if self.z_controller_on else 0,)

    def ZCtrl_OnOffSet(self, Z_controller_status: int) -> tuple[Any, ...]:
        self._record("ZCtrl_OnOffSet", Z_controller_status)
        self.z_controller_on = bool(int(Z_controller_status))
        self.z_status_code = 2 if self.z_controller_on else 1
        return ()

    def ZCtrl_LimitsEnabledGet(self) -> tuple[Any, ...]:
        self._record("ZCtrl_LimitsEnabledGet")
        return (1 if self.z_limits_enabled else 0,)

    def ZCtrl_HomePropsGet(self) -> tuple[Any, ...]:
        self._record("ZCtrl_HomePropsGet")
        return (int(self.home_relative), self.home_position_m)

    def ZCtrl_Home(self) -> tuple[Any, ...]:
        self._record("ZCtrl_Home")
        self.z_position_m = (self.z_position_m + self.home_position_m
                             if self.home_relative else self.home_position_m)
        # Model the documented position command, not unverified feedback changes.
        return ()

    def ZCtrl_LimitsGet(self) -> tuple[Any, ...]:
        self._record("ZCtrl_LimitsGet")
        return (self.z_high_limit_m, self.z_low_limit_m)

    def ZCtrl_TipLiftGet(self) -> tuple[Any, ...]:
        self._record("ZCtrl_TipLiftGet")
        return (self.tip_lift_m,)

    def ZCtrl_SetpntGet(self) -> tuple[Any, ...]:
        return (self.z_setpoint_a,)

    def ZCtrl_SetpntSet(self, Setpoint_A: float) -> tuple[Any, ...]:
        self._record("ZCtrl_SetpntSet", Setpoint_A)
        self.z_setpoint_a = float(Setpoint_A)
        return ()

    def ZCtrl_Withdraw(self, Wait_until_finished: int, Timeout_ms: int) -> tuple[Any, ...]:
        self._record("ZCtrl_Withdraw", Wait_until_finished, Timeout_ms)
        self.z_status_code = 6
        return ()

    def Scan_StatusGet(self) -> tuple[Any, ...]:
        return (1 if self.scan_running else 0,)

    def Scan_FrameGet(self) -> tuple[Any, ...]:
        return self.scan_frame

    def Scan_FrameSet(
        self,
        Center_X_m: float,
        Center_Y_m: float,
        Width_m: float,
        Height_m: float,
        Angle_deg: float,
    ) -> tuple[Any, ...]:
        self._record(
            "Scan_FrameSet",
            Center_X_m,
            Center_Y_m,
            Width_m,
            Height_m,
            Angle_deg,
        )
        self.scan_frame = (
            float(Center_X_m),
            float(Center_Y_m),
            float(Width_m),
            float(Height_m),
            float(Angle_deg),
        )
        forward, backward, ft, bt, keep, ratio = self.scan_speed
        if keep == 1:
            self.scan_speed = (Width_m/ft, Width_m/bt, ft, bt, keep, ratio)
        elif keep == 0:
            self.scan_speed = (forward, backward, Width_m/forward, Width_m/backward, keep, ratio)
        return ()

    def Scan_BufferGet(self) -> tuple[Any, ...]:
        count, indexes, pixels, lines = self.scan_buffer
        return (count, list(indexes), pixels, lines)

    def Scan_SpeedGet(self) -> tuple[Any, ...]:
        self._record("Scan_SpeedGet")
        return self.scan_speed

    def Scan_SpeedSet(self, forward, backward, forward_time, backward_time, keep, ratio):
        self._record("Scan_SpeedSet", forward, backward, forward_time, backward_time, keep, ratio)
        self.scan_speed = (float(forward), float(backward), float(forward_time), float(backward_time),
                           int(keep)-1, float(ratio))
        return ()

    def Piezo_RangeGet(self):
        self._record("Piezo_RangeGet")
        return self.piezo_range

    def Piezo_DriftCompGet(self):
        self._record("Piezo_DriftCompGet")
        return self.drift_compensation

    def Piezo_DriftCompSet(self, Compensation_on_off, Vx_m_s, Vy_m_s, Vz_m_s, Sat_Lim):
        self._record("Piezo_DriftCompSet", Compensation_on_off, Vx_m_s, Vy_m_s, Vz_m_s, Sat_Lim)
        if Compensation_on_off not in (0, 1, 2):
            raise ValueError("Invalid drift compensation Set enum")
        before = self.drift_compensation
        if Sat_Lim is None:
            if len(before) != 7 or (Compensation_on_off, Vx_m_s, Vy_m_s, Vz_m_s) != (2, 0., 0., 0.):
                raise ValueError("Seven-field drift protocol supports only OFF/zero-speed requests")
            self.drift_compensation = (0, 0., 0., 0., *before[4:7])
            return ()
        enabled = before[0] if Compensation_on_off == 0 else int(Compensation_on_off == 1)
        # Deterministic configuration model only: no wall-clock piezo motion or
        # physical compensation claim. Saturation can be injected by tests.
        self.drift_compensation = (enabled, float(Vx_m_s), float(Vy_m_s), float(Vz_m_s),
                                   *before[4:7], float(Sat_Lim))
        return ()

    def LockIn_ModOnOffGet(self, number):
        self._record("LockIn_ModOnOffGet", number)
        return (int(self.lock_in_enabled),)

    def LockIn_ModSignalGet(self, number):
        self._record("LockIn_ModSignalGet", number)
        return (self.lock_in_signal_index,)

    def LockIn_ModAmpGet(self, number):
        self._record("LockIn_ModAmpGet", number)
        return (self.lock_in_amplitude,)

    def LockIn_ModPhasRegGet(self, number):
        self._record("LockIn_ModPhasRegGet", number)
        return (self.lock_in_phase_register,)

    def LockIn_ModPhasFreqGet(self, register):
        self._record("LockIn_ModPhasFreqGet", register)
        return (self.lock_in_phase_frequencies[register],)

    def LockIn_ModHarmonicGet(self, number):
        self._record("LockIn_ModHarmonicGet", number)
        return (self.lock_in_harmonic,)

    def LockIn_ModPhasGet(self, number):
        self._record("LockIn_ModPhasGet", number)
        return (self.lock_in_phase_deg,)

    def LockIn_DemodSignalGet(self, number):
        self._record("LockIn_DemodSignalGet", number)
        return (self.lock_in_demod_signal_index,)

    def LockIn_DemodHarmonicGet(self, number):
        self._record("LockIn_DemodHarmonicGet", number)
        return (self.lock_in_demod_harmonic,)

    def LockIn_DemodPhasRegGet(self, number):
        self._record("LockIn_DemodPhasRegGet", number)
        return (self.lock_in_demod_phase_register,)

    def LockIn_DemodPhasGet(self, number):
        self._record("LockIn_DemodPhasGet", number)
        return (self.lock_in_demod_phase_deg,)

    def LockIn_DemodHPFilterGet(self, number):
        self._record("LockIn_DemodHPFilterGet", number)
        return self.lock_in_demod_high_pass

    def LockIn_DemodLPFilterGet(self, number):
        self._record("LockIn_DemodLPFilterGet", number)
        return self.lock_in_demod_low_pass

    def LockIn_DemodSyncFilterGet(self, number):
        self._record("LockIn_DemodSyncFilterGet", number)
        return (self.lock_in_demod_sync_filter,)

    def LockIn_DemodRTSignalsGet(self, number):
        self._record("LockIn_DemodRTSignalsGet", number)
        return (self.lock_in_demod_rt_signals,)

    def Scan_XYPosGet(self, Wait_newest_data: int) -> tuple[Any, ...]:
        self._record("Scan_XYPosGet", Wait_newest_data)
        return self.scan_xy_position

    def Scan_FrameDataGrab(self, Channel_index: int, Data_direction: int) -> tuple[Any, ...]:
        self._record("Scan_FrameDataGrab", Channel_index, Data_direction)
        rows = len(self.frame_data)
        columns = len(self.frame_data[0]) if rows else 0
        return (11, "Current (A)", rows, columns, [list(row) for row in self.frame_data], int(self.scan_direction_up))

    def Scan_Action(self, Scan_action: int, Scan_direction: int) -> tuple[Any, ...]:
        self._record("Scan_Action", Scan_action, Scan_direction)
        if Scan_action == 0:
            self.scan_direction_up = bool(Scan_direction)
            self.scan_running = True
            self.scan_paused = False
        elif Scan_action == 1:
            self.scan_running = False
            self.scan_paused = False
        elif Scan_action == 2 and self.scan_running:
            self.scan_running = False
            self.scan_paused = True
        elif Scan_action == 3 and self.scan_paused:
            self.scan_direction_up = bool(Scan_direction)
            self.scan_running = True
            self.scan_paused = False
        return ()

    def Scan_WaitEndOfScan(self, Timeout_ms: int) -> tuple[Any, ...]:
        self._record("Scan_WaitEndOfScan", Timeout_ms)
        self.scan_running = False
        return (0, 0, "")

    def Signals_NamesGet(self) -> tuple[Any, ...]:
        return (sum(len(name) for name in self.signal_names), len(self.signal_names), list(self.signal_names))

    def Signals_ValsGet(self, Signals_indexes: Sequence[int], Wait_for_newest_data: int) -> tuple[Any, ...]:
        self._record("Signals_ValsGet", list(Signals_indexes), Wait_for_newest_data)
        values = [self.signal_values.get(int(index), float("nan")) for index in Signals_indexes]
        return (len(values), values)

    def DataLog_ChsGet(self) -> tuple[Any, ...]:
        self._record("DataLog_ChsGet")
        indexes = list(self.data_log_channel_indexes)
        return (len(indexes), indexes)

    def DataLog_ChsSet(self, Channel_indexes: Sequence[int]) -> tuple[Any, ...]:
        indexes = [int(index) for index in Channel_indexes]
        self._record("DataLog_ChsSet", indexes)
        self.data_log_channel_indexes = indexes
        return ()

    def DataLog_PropsGet(self) -> tuple[Any, ...]:
        self._record("DataLog_PropsGet")
        hours, remainder = divmod(self.data_log_duration_s, 3600.0)
        minutes, seconds = divmod(remainder, 60.0)
        return (
            1 if self.data_log_timed else 0,
            int(hours),
            int(minutes),
            float(seconds),
            self.data_log_averaging,
            len(self.data_log_basename.encode("utf-8")),
            self.data_log_basename,
            len(self.data_log_comment.encode("utf-8")),
            self.data_log_comment,
        )

    def DataLog_PropsSet(
        self,
        Acquisition_mode: int,
        Acquisition_duration_hours: int,
        Acquisition_duration_minutes: int,
        Acquisition_duration_seconds: float,
        Averaging: int,
        Basename: str,
        Comment: str,
        List_of_modules: Sequence[str],
    ) -> tuple[Any, ...]:
        self._record(
            "DataLog_PropsSet",
            Acquisition_mode,
            Acquisition_duration_hours,
            Acquisition_duration_minutes,
            Acquisition_duration_seconds,
            Averaging,
            Basename,
            Comment,
            list(List_of_modules),
        )
        if int(Acquisition_mode) in {1, 2}:
            self.data_log_timed = int(Acquisition_mode) == 2
        self.data_log_duration_s = (
            int(Acquisition_duration_hours) * 3600.0
            + int(Acquisition_duration_minutes) * 60.0
            + float(Acquisition_duration_seconds)
        )
        if int(Averaging) > 0:
            self.data_log_averaging = int(Averaging)
        requested_basename = str(Basename)
        if requested_basename != self.data_log_basename:
            self.data_log_saved_file_path = ""
            self.data_log_saved_points = 0
        self.data_log_basename = requested_basename
        self.data_log_comment = str(Comment)
        self.data_log_modules = [str(module) for module in List_of_modules]
        return ()

    def DataLog_StatusGet(self) -> tuple[Any, ...]:
        self._record("DataLog_StatusGet")
        hours, remainder = divmod(self.data_log_elapsed_s, 3600.0)
        minutes, seconds = divmod(remainder, 60.0)
        return (
            len(self.data_log_start_time.encode("utf-8")),
            self.data_log_start_time,
            int(hours),
            int(minutes),
            float(seconds),
            len(self.data_log_stop_time.encode("utf-8")),
            self.data_log_stop_time,
            len(self.data_log_saved_file_path.encode("utf-8")),
            self.data_log_saved_file_path,
            self.data_log_saved_points,
        )

    def DataLog_Start(self) -> tuple[Any, ...]:
        self._record("DataLog_Start")
        self.data_log_running = True
        self.data_log_start_time = "2026-07-29 12:00:00"
        self.data_log_stop_time = ""
        return ()

    def DataLog_Stop(self) -> tuple[Any, ...]:
        self._record("DataLog_Stop")
        self.data_log_running = False
        self.data_log_stop_time = "2026-07-29 12:00:01"
        if not self.data_log_saved_file_path:
            self.data_log_saved_file_path = str(
                Path(self.session_path) / f"{self.data_log_basename}00001.dat"
            )
        return ()

    def AutoApproach_OnOffGet(self) -> tuple[Any, ...]:
        self._record("AutoApproach_OnOffGet")
        return (1 if self.auto_approach_running else 0,)

    def AutoApproach_OnOffSet(self, OnOff_status: int) -> tuple[Any, ...]:
        self._record("AutoApproach_OnOffSet", OnOff_status)
        self.auto_approach_running = bool(int(OnOff_status))
        if self.auto_approach_running and self.auto_approach_complete_on_start:
            self.auto_approach_running = False
            self.z_controller_on = True
            self.z_status_code = 2
        return ()

    def SpectrumAnlzr_ChGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]:
        self._record("SpectrumAnlzr_ChGet", Spectrum_Analyzer_instance)
        return (self.spectrum_channel_indexes[Spectrum_Analyzer_instance],)

    def SpectrumAnlzr_FreqRangeGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]:
        self._record("SpectrumAnlzr_FreqRangeGet", Spectrum_Analyzer_instance)
        selected, values = self.spectrum_frequency_ranges[Spectrum_Analyzer_instance]
        return (selected, len(values), list(values))

    def SpectrumAnlzr_FreqResGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]:
        self._record("SpectrumAnlzr_FreqResGet", Spectrum_Analyzer_instance)
        selected, values = self.spectrum_frequency_resolutions[Spectrum_Analyzer_instance]
        return (selected, len(values), list(values))

    def SpectrumAnlzr_FFTWindowGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]:
        self._record("SpectrumAnlzr_FFTWindowGet", Spectrum_Analyzer_instance)
        return (self.spectrum_fft_windows[Spectrum_Analyzer_instance],)

    def SpectrumAnlzr_AveragGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]:
        self._record("SpectrumAnlzr_AveragGet", Spectrum_Analyzer_instance)
        return self.spectrum_averaging[Spectrum_Analyzer_instance]

    def SpectrumAnlzr_ACCouplingGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]:
        self._record("SpectrumAnlzr_ACCouplingGet", Spectrum_Analyzer_instance)
        return (1 if self.spectrum_ac_coupling[Spectrum_Analyzer_instance] else 0,)

    def SpectrumAnlzr_DataGet(self, Spectrum_Analyzer_instance: int) -> tuple[Any, ...]:
        self._record("SpectrumAnlzr_DataGet", Spectrum_Analyzer_instance)
        f0_hz, df_hz, density = self.spectrum_data[Spectrum_Analyzer_instance]
        return (f0_hz, df_hz, len(density), list(density))

    def FolMe_XYPosGet(self, Wait_for_newest_data: int) -> tuple[Any, ...]:
        self._record("FolMe_XYPosGet", Wait_for_newest_data)
        return self.follow_me_position

    def FolMe_XYPosSet(self, X_m: float, Y_m: float, Wait_end_of_move: int) -> tuple[Any, ...]:
        self._record("FolMe_XYPosSet", X_m, Y_m, Wait_end_of_move)
        self.follow_me_position = (float(X_m), float(Y_m))
        return ()

    def FolMe_SpeedGet(self) -> tuple[Any, ...]:
        self._record("FolMe_SpeedGet")
        speed, custom = self.follow_me_speed
        return (speed, 1 if custom else 0)

    def FolMe_Stop(self) -> tuple[Any, ...]:
        self._record("FolMe_Stop")
        return ()

    def BiasSpectr_StatusGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_StatusGet")
        return (1 if self.bias_spectroscopy_running else 0,)

    def BiasSpectr_AdvPropsGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_AdvPropsGet")
        return self.bias_spectroscopy_advanced

    def BiasSpectr_AdvPropsSet(self, *args: int) -> tuple[Any, ...]:
        if len(args) != 4 or any(v not in (0, 1, 2) for v in args):
            raise ValueError("Advanced properties require four no-change/on/off codes")
        self._record("BiasSpectr_AdvPropsSet", *args)
        self.bias_spectroscopy_advanced = tuple(
            old if new == 0 else int(new == 1)
            for old, new in zip(self.bias_spectroscopy_advanced, args))
        return ()

    def BiasSpectr_MLSModeGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_MLSModeGet")
        return (len(self.bias_spectroscopy_sweep_mode), self.bias_spectroscopy_sweep_mode)

    def BiasSpectr_MLSLockinPerSegGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_MLSLockinPerSegGet")
        return (self.bias_spectroscopy_lock_in_per_segment,)

    def BiasSpectr_PropsGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_PropsGet")
        parameters = list(self.bias_spectroscopy_parameter_names)
        fixed = list(self.bias_spectroscopy_fixed_parameter_names)
        return (
            1 if self.bias_spectroscopy_save_all else 0,
            self.bias_spectroscopy_sweeps,
            1 if self.bias_spectroscopy_backward else 0,
            self.bias_spectroscopy_points,
            sum(map(len, parameters)),
            len(parameters),
            parameters,
            sum(map(len, fixed)),
            len(fixed),
            fixed,
            int(self.bias_spectroscopy_autosave),
            int(self.bias_spectroscopy_show_save_dialog),
        )

    def BiasSpectr_PropsSet(self, *args: Any) -> tuple[Any, ...]:
        if len(args) != 7:
            raise ValueError("BiasSpectr_PropsSet expects seven values")
        self._record("BiasSpectr_PropsSet", *args)
        if args[0]: self.bias_spectroscopy_save_all = int(args[0]) == 1
        if args[1]: self.bias_spectroscopy_sweeps = int(args[1])
        if args[2]: self.bias_spectroscopy_backward = int(args[2]) == 1
        if args[3]: self.bias_spectroscopy_points = int(args[3])
        timing = list(self.bias_spectroscopy_timing)
        timing[1] = float(args[4])
        self.bias_spectroscopy_timing = tuple(timing)
        if args[5]: self.bias_spectroscopy_autosave = int(args[5]) == 1
        if args[6]: self.bias_spectroscopy_show_save_dialog = int(args[6]) == 1
        return ()

    def BiasSpectr_ChsGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_ChsGet")
        names = list(self.bias_spectroscopy_channel_names)
        indexes = list(self.bias_spectroscopy_channel_indexes)
        return (len(indexes), indexes, sum(map(len, names)), len(names), names)

    def BiasSpectr_ChsSet(self, Channel_indexes: list[int]) -> tuple[Any, ...]:
        indexes = [int(index) for index in Channel_indexes]
        self._record("BiasSpectr_ChsSet", indexes)
        self.bias_spectroscopy_channel_indexes = indexes
        available = ["Bias (V)", "Current (A)", "Z (m)", "Input 2 (V)"]
        self.bias_spectroscopy_channel_names = [
            available[index] if 0 <= index < len(available) else f"Channel {index}" for index in indexes
        ]
        return ()

    def BiasSpectr_LimitsGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_LimitsGet")
        return self.bias_spectroscopy_limits

    def BiasSpectr_LimitsSet(self, Start_value_V: float, End_value_V: float) -> tuple[Any, ...]:
        self._record("BiasSpectr_LimitsSet", Start_value_V, End_value_V)
        self.bias_spectroscopy_limits = (float(Start_value_V), float(End_value_V))
        return ()

    def BiasSpectr_TimingGet(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_TimingGet")
        return self.bias_spectroscopy_timing

    def BiasSpectr_TimingSet(self, *args: float) -> tuple[Any, ...]:
        if len(args) != 8:
            raise ValueError("BiasSpectr_TimingSet expects eight timing values")
        self._record("BiasSpectr_TimingSet", *args)
        self.bias_spectroscopy_timing = tuple(float(value) for value in args)
        return ()

    def BiasSpectr_Start(self, Get_data: int, Save_base_name: str) -> tuple[Any, ...]:
        self._record("BiasSpectr_Start", Get_data, Save_base_name)
        self.bias_spectroscopy_running = True
        start, end = self.bias_spectroscopy_limits
        points = self.bias_spectroscopy_points
        denominator = max(points - 1, 1)
        bias_axis = [start + (end - start) * index / denominator for index in range(points)]
        rows: list[list[float]] = []
        for row_index, name in enumerate(self.bias_spectroscopy_channel_names):
            if "Bias" in name:
                rows.append(bias_axis)
            else:
                rows.append([(row_index + 1) * (value * value + 1e-6) for value in bias_axis])
        self.bias_spectroscopy_running = False
        if not Get_data:
            return (0, 0, [], 0, 0, [], 0, [])
        names = list(self.bias_spectroscopy_channel_names)
        parameters = [self.follow_me_position[0], self.follow_me_position[1]]
        return (
            sum(map(len, names)),
            len(names),
            names,
            len(rows),
            points,
            rows,
            len(parameters),
            parameters,
        )

    def BiasSpectr_Stop(self) -> tuple[Any, ...]:
        self._record("BiasSpectr_Stop")
        self.bias_spectroscopy_running = False
        return ()

    def TipShaper_PropsGet(self) -> tuple[Any, ...]:
        self._record("TipShaper_PropsGet")
        return self.tip_shaper_properties

    def TipShaper_PropsSet(self, *args: Any) -> tuple[Any, ...]:
        if len(args) != 11:
            raise ValueError("TipShaper_PropsSet expects eleven values")
        self._record("TipShaper_PropsSet", *args)
        self.tip_shaper_properties = tuple(args)
        return ()

    def TipShaper_Start(self, Wait_until_finished: int, Timeout_ms: int) -> tuple[Any, ...]:
        self._record("TipShaper_Start", Wait_until_finished, Timeout_ms)
        initial_bias_v = self.bias_v
        change_bias = int(self.tip_shaper_properties[1])
        if change_bias == 1:
            self.bias_v = float(self.tip_shaper_properties[2])
            self.tip_shaper_bias_trace.append(self.bias_v)
        self.bias_v = float(self.tip_shaper_properties[5])
        self.tip_shaper_bias_trace.append(self.bias_v)
        self.bias_v = initial_bias_v
        self.tip_shaper_bias_trace.append(self.bias_v)
        return ()

    def AtomTrack_StatusGet(self, AT_control: int) -> tuple[Any, ...]:
        self._record("AtomTrack_StatusGet", AT_control)
        return (1 if self.atom_tracking_status.get(int(AT_control), False) else 0,)

    def AtomTrack_PropsGet(self) -> tuple[Any, ...]:
        self._record("AtomTrack_PropsGet")
        return self.atom_tracking_properties

    def AtomTrack_CtrlSet(self, AT_control: int, Status: int) -> tuple[Any, ...]:
        self._record("AtomTrack_CtrlSet", AT_control, Status)
        self.atom_tracking_status[int(AT_control)] = bool(Status)
        return ()

    def Motor_StopMove(self) -> tuple[Any, ...]:
        self._record("Motor_StopMove")
        return ()

    def Motor_PosGet(self, Group: int, Timeout: int) -> tuple[Any, ...]:
        group = int(Group)
        self._record("Motor_PosGet", group, int(Timeout))
        return tuple(self.motor_positions[group])

    def Motor_StartMove(
        self,
        Direction: int,
        Number_of_steps: int,
        Group: int,
        Wait_until_finished: int,
    ) -> tuple[Any, ...]:
        direction = int(Direction)
        steps = int(Number_of_steps)
        group = int(Group)
        self._record(
            "Motor_StartMove",
            direction,
            steps,
            group,
            int(Wait_until_finished),
        )
        if direction not in {0, 1, 2, 3, 4}:
            raise ValueError("fake motor supports XY and Z+ retraction only; Z- is forbidden")
        axis = direction // 2
        sign = 1 if direction % 2 == 0 else -1
        self.motor_step_counts[group][axis] += sign * steps
        return ()
