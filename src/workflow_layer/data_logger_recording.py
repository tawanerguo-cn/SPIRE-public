from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from instrument_layer import DataLogRecipe, DataLogStatus, NanonisDevice


@dataclass(frozen=True)
class DataLoggerSegmentCapture:
    """Terminal Nanonis Data Logger evidence for one IXYZ segment."""

    segment_id: int
    basename: str
    saved_file_path: Path | None
    saved_points: int
    elapsed_s: float


class DataLoggerSegmentController:
    """Bind the existing typed Data Logger API to high-resolution segments.

    This workflow does not bypass ``SafetyGate``. Consequently it is fully
    testable with the fake backend while live configure/start remain disabled
    until the lab explicitly commissions those bounded actions.
    """

    def __init__(
        self,
        device: NanonisDevice,
        *,
        signals: tuple[str, ...],
        run_id: str,
    ) -> None:
        self.device = device
        self.signals = signals
        self.run_id = run_id
        self._active_segment_id: int | None = None
        self._active_basename: str | None = None

    @property
    def running(self) -> bool:
        return self._active_segment_id is not None

    def start_segment(self, segment_id: int) -> DataLogStatus:
        if self.running:
            raise RuntimeError("a Data Logger segment is already active")
        basename = _segment_basename(self.run_id, segment_id)
        self.device.configure_data_log(
            DataLogRecipe(
                signals=self.signals,
                basename=basename,
                comment=(
                    f"SPiRE run={self.run_id} segment={segment_id}; "
                    "IXYZ acquisition-complete source"
                ),
                averaging=1,
            )
        )
        self._active_segment_id = int(segment_id)
        self._active_basename = basename
        status = self.device.start_data_log()
        if not status.running:
            raise RuntimeError("Data Logger did not report running after start")
        return status

    def stop_segment(self) -> DataLoggerSegmentCapture | None:
        if not self.running:
            return None
        segment_id = self._active_segment_id
        basename = self._active_basename
        try:
            status = self.device.stop_data_log()
            if status.running:
                raise RuntimeError(
                    "Data Logger still reports running after stop"
                )
            return DataLoggerSegmentCapture(
                segment_id=int(segment_id),
                basename=str(basename),
                saved_file_path=(
                    None
                    if status.saved_file_path is None
                    else Path(status.saved_file_path).expanduser().resolve()
                ),
                saved_points=int(status.saved_points),
                elapsed_s=float(status.elapsed_s),
            )
        finally:
            self._active_segment_id = None
            self._active_basename = None


def _segment_basename(run_id: str, segment_id: int) -> str:
    suffix = f"_s{int(segment_id):04d}"
    prefix = "SPiRE_"
    maximum_run_id_length = 127 - len(prefix) - len(suffix)
    return f"{prefix}{run_id[:maximum_run_id_length]}{suffix}"
