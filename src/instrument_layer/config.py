from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import tomllib


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "nanonis.toml"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6501
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_BACKEND = "fake"


@dataclass(frozen=True)
class NanonisConnectionConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    timeout_s: float = DEFAULT_TIMEOUT_S
    backend: str = DEFAULT_BACKEND


def load_connection_config(path: str | Path | None = None) -> NanonisConnectionConfig:
    environment_path = os.environ.get("SPIRE_CONNECTION_CONFIG", "").strip()
    if path is not None:
        config_path = Path(path).expanduser()
        required = True
    elif environment_path:
        config_path = Path(environment_path).expanduser()
        required = True
    else:
        config_path = DEFAULT_CONFIG_PATH
        required = False
    raw: dict[str, object] = {}
    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        if required:
            raise FileNotFoundError(
                f"Nanonis connection config does not exist: {config_path}"
            ) from None
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid TOML in Nanonis connection config {config_path}: {error}") from error
    else:
        table = data.get("connection")
        if table is None:
            raise ValueError(f"[{config_path}] must define a [connection] table")
        if not isinstance(table, dict):
            raise ValueError(f"[{config_path}] [connection] must be a TOML table")
        raw = table

    host = _environment_or_value("NANONIS_HOST", raw, "host", DEFAULT_HOST)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Nanonis host must be a nonempty string")
    host = host.strip()

    port_value = _environment_or_value("NANONIS_PORT", raw, "port", DEFAULT_PORT)
    try:
        if isinstance(port_value, bool):
            raise ValueError
        port = int(port_value)
        if isinstance(port_value, float) and port_value != port:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Nanonis port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("Nanonis port must be an integer from 1 to 65535")

    timeout_value = _environment_or_value(
        "NANONIS_TIMEOUT_S", raw, "timeout_s", DEFAULT_TIMEOUT_S
    )
    try:
        if isinstance(timeout_value, bool):
            raise ValueError
        timeout_s = float(timeout_value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Nanonis timeout_s must be a finite positive number") from None
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("Nanonis timeout_s must be a finite positive number")

    backend_value = _environment_or_value("NANONIS_BACKEND", raw, "backend", DEFAULT_BACKEND)
    if not isinstance(backend_value, str):
        raise ValueError("Nanonis backend must be 'fake' or 'live'")
    backend = backend_value.strip().lower()
    if backend not in {"fake", "live"}:
        raise ValueError("Nanonis backend must be 'fake' or 'live'")
    return NanonisConnectionConfig(
        host=host,
        port=port,
        timeout_s=timeout_s,
        backend=backend,
    )


def _environment_or_value(name: str, values: dict[str, object], key: str, default):
    """Keep field-specific environment overrides while validating both sources."""
    if name in os.environ:
        return os.environ[name]
    return values.get(key, default)
