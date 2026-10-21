from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path
import re

import numpy as np
from numpy.typing import NDArray

from instrument_layer import ScanFrame


FloatArray = NDArray[np.floating]


@dataclass(frozen=True)
class NanonisSxmChannel:
    index: int
    name: str
    unit: str
    direction: str
    calibration: float
    offset: float
    forward: FloatArray | None
    backward: FloatArray | None


@dataclass(frozen=True)
class NanonisSxmFile:
    path: Path
    sha256: str
    rows: int
    columns: int
    range_x_m: float
    range_y_m: float
    offset_x_m: float
    offset_y_m: float
    recorded_date: str
    recorded_time: str
    scan_direction: str
    bias_v: float
    z_setpoint_a: float | None
    channels: tuple[NanonisSxmChannel, ...]
    scan_angle_deg: float | None = None
    header: tuple[tuple[str, str], ...] = ()

    @property
    def scan_frame(self) -> ScanFrame:
        if self.scan_angle_deg is None:
            raise ValueError("SXM has no SCAN_ANGLE; physical targeting is unavailable")
        return ScanFrame(
            self.offset_x_m, self.offset_y_m,
            self.range_x_m, self.range_y_m, self.scan_angle_deg,
        )

    def header_value(self, name: str) -> str:
        for key, value in self.header:
            if key.casefold() == name.casefold():
                return value
        raise KeyError(name)

    def oriented_channel(self, name: str, direction: str = "forward") -> FloatArray:
        """Return top-origin pixels, x increasing right, y decreasing down.

        Stored arrays remain in acquisition order. Nanonis' supplied loadsxm.py
        documents up scans starting bottom-left and retrace starting at the right.
        This method is the single SXM orientation boundary used for targeting.
        """
        if direction not in {"forward", "backward"}:
            raise ValueError("direction must be forward or backward")
        channel = self.channel(name)
        data = getattr(channel, direction)
        if data is None:
            raise ValueError(f"{name} has no {direction} data")
        if direction == "backward":
            data = np.fliplr(data)
        if self.scan_direction == "up":
            data = np.flipud(data)
        elif self.scan_direction != "down":
            raise ValueError("unknown SXM scan direction")
        return data

    def channel(self, name: str) -> NanonisSxmChannel:
        requested = name.casefold()
        for channel in self.channels:
            if channel.name.casefold() == requested:
                return channel
        raise KeyError(name)


@dataclass(frozen=True)
class NanonisDatFile:
    path: Path
    sha256: str
    header: tuple[tuple[str, str], ...]
    columns: tuple[str, ...]
    data: FloatArray

    def header_value(self, name: str) -> str:
        requested = name.casefold()
        for key, value in self.header:
            if key.casefold() == requested:
                return value
        raise KeyError(name)

    def column(self, name: str) -> FloatArray:
        requested = name.casefold()
        for index, column_name in enumerate(self.columns):
            if column_name.casefold() == requested:
                return self.data[:, index]
        raise KeyError(name)


def read_nanonis_sxm(path: str | Path) -> NanonisSxmFile:
    source = Path(path)
    payload = source.read_bytes()
    end_offset = payload.find(b":SCANIT_END:")
    if end_offset < 0:
        raise ValueError(f"{source} has no :SCANIT_END: marker")
    binary_marker = payload.find(b"\x1a\x04", end_offset)
    if binary_marker < 0:
        raise ValueError(f"{source} has no Nanonis binary marker")
    header = payload[:end_offset].decode("latin-1")
    sections = tuple(
        (match.group(1), match.group(2).strip())
        for match in re.finditer(r"^:([^\r\n]+):\s*\r?\n(.*?)(?=^:[^\r\n]+:|\Z)", header, re.M | re.S)
    )
    metadata = dict(sections)
    if "SCANIT_TYPE" in metadata and metadata["SCANIT_TYPE"].split() != ["FLOAT", "MSBFIRST"]:
        raise ValueError("only FLOAT MSBFIRST SXM data are supported")
    columns, rows = _integer_pair(_section(header, "SCAN_PIXELS"))
    if rows <= 0 or columns <= 0:
        raise ValueError("SXM dimensions must be positive")
    range_x_m, range_y_m = _float_pair(_section(header, "SCAN_RANGE"))
    offset_x_m, offset_y_m = _float_pair(_section(header, "SCAN_OFFSET"))
    if not all(math.isfinite(value) for value in (range_x_m, range_y_m, offset_x_m, offset_y_m)):
        raise ValueError("SXM geometry must be finite")
    if range_x_m <= 0 or range_y_m <= 0:
        raise ValueError("SXM scan ranges must be positive")
    angle = float(metadata["SCAN_ANGLE"]) if "SCAN_ANGLE" in metadata else None
    if angle is not None and not math.isfinite(angle):
        raise ValueError("SXM angle must be finite")
    channel_metadata = _data_info(_section(header, "DATA_INFO"))
    frame_count = sum(2 if item[3] == "both" else 1 for item in channel_metadata)
    expected_values = frame_count * rows * columns
    binary = payload[binary_marker + 2 :]
    if len(binary) != expected_values * 4:
        raise ValueError(
            f"{source} binary payload has {len(binary)} bytes; "
            f"expected {expected_values * 4}"
        )
    values = np.frombuffer(binary, dtype=">f4").astype(np.float32, copy=True)
    channels: list[NanonisSxmChannel] = []
    cursor = 0
    frame_values = rows * columns
    for index, name, unit, direction, calibration, offset in channel_metadata:
        first = values[cursor : cursor + frame_values].reshape(rows, columns)
        cursor += frame_values
        second = None
        if direction == "both":
            second = values[cursor : cursor + frame_values].reshape(rows, columns)
            cursor += frame_values
        if direction == "backward":
            forward, backward = None, first
        else:
            forward, backward = first, second
        if forward is not None:
            forward.setflags(write=False)
        if backward is not None:
            backward.setflags(write=False)
        channels.append(
            NanonisSxmChannel(
                index=index,
                name=name,
                unit=unit,
                direction=direction,
                calibration=calibration,
                offset=offset,
                forward=forward,
                backward=backward,
            )
        )
    return NanonisSxmFile(
        path=source,
        sha256=sha256(payload).hexdigest(),
        rows=rows,
        columns=columns,
        range_x_m=range_x_m,
        range_y_m=range_y_m,
        offset_x_m=offset_x_m,
        offset_y_m=offset_y_m,
        recorded_date=_section(header, "REC_DATE").strip(),
        recorded_time=_section(header, "REC_TIME").strip(),
        scan_direction=_section(header, "SCAN_DIR").strip().casefold(),
        bias_v=float(_section(header, "BIAS")),
        z_setpoint_a=_z_setpoint(_section(header, "Z-CONTROLLER")),
        channels=tuple(channels),
        scan_angle_deg=angle,
        header=sections,
    )


def read_nanonis_dat(path: str | Path) -> NanonisDatFile:
    source = Path(path)
    raw = source.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines()
    try:
        data_marker = lines.index("[DATA]")
    except ValueError as exc:
        raise ValueError(f"{source} has no [DATA] marker") from exc
    if data_marker + 1 >= len(lines):
        raise ValueError(f"{source} has no data-column row")
    header: list[tuple[str, str]] = []
    for line in lines[:data_marker]:
        cells = [cell.strip() for cell in line.split("\t")]
        if cells and cells[0]:
            header.append((cells[0], cells[1] if len(cells) > 1 else ""))
    columns = tuple(cell.strip() for cell in lines[data_marker + 1].split("\t"))
    if not all(columns) or len(set(columns)) != len(columns):
        raise ValueError("DAT column names must be non-empty and unique")
    rows: list[list[float]] = []
    for line_number, line in enumerate(lines[data_marker + 2 :], data_marker + 3):
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) != len(columns):
            raise ValueError(
                f"{source}:{line_number} has {len(cells)} values; "
                f"expected {len(columns)}"
            )
        rows.append([float(cell) for cell in cells])
    if not rows:
        raise ValueError(f"{source} contains no numeric data rows")
    data = np.asarray(rows, dtype=np.float64)
    data.setflags(write=False)
    return NanonisDatFile(
        path=source,
        sha256=sha256(raw).hexdigest(),
        header=tuple(header),
        columns=columns,
        data=data,
    )


def _section(header: str, name: str) -> str:
    marker = f":{name}:"
    offset = header.find(marker)
    if offset < 0:
        raise ValueError(f"missing SXM header section {marker}")
    start = offset + len(marker)
    end = header.find("\n:", start)
    if end < 0:
        end = len(header)
    return header[start:end].strip("\r\n")


def _integer_pair(value: str) -> tuple[int, int]:
    parts = value.split()
    if len(parts) != 2:
        raise ValueError(f"expected two integers, got {value!r}")
    return int(parts[0]), int(parts[1])


def _float_pair(value: str) -> tuple[float, float]:
    parts = value.split()
    if len(parts) != 2:
        raise ValueError(f"expected two floats, got {value!r}")
    return float(parts[0]), float(parts[1])


def _data_info(
    value: str,
) -> tuple[tuple[int, str, str, str, float, float], ...]:
    result: list[tuple[int, str, str, str, float, float]] = []
    for line in value.splitlines():
        cells = [cell.strip() for cell in line.split("\t") if cell.strip()]
        if not cells or cells[0].casefold() == "channel":
            continue
        if len(cells) < 6:
            raise ValueError(f"invalid DATA_INFO row {line!r}")
        direction = cells[3].casefold()
        if direction not in {"both", "forward", "backward"}:
            raise ValueError(f"unsupported SXM channel direction {cells[3]!r}")
        result.append(
            (
                int(cells[0]),
                cells[1],
                cells[2],
                direction,
                float(cells[4]),
                float(cells[5]),
            )
        )
    if not result:
        raise ValueError("SXM DATA_INFO contains no channels")
    return tuple(result)


def _z_setpoint(value: str) -> float | None:
    for line in value.splitlines():
        cells = [cell.strip() for cell in line.split("\t") if cell.strip()]
        if len(cells) < 3 or cells[0].casefold() in {"name", ""}:
            continue
        token = cells[2].split()[0]
        try:
            return float(token)
        except ValueError:
            continue
    return None
