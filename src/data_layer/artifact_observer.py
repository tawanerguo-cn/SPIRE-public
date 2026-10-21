from __future__ import annotations

from dataclasses import dataclass
import os
import stat as stat_module
from pathlib import Path
from threading import Event, Thread
import time
from typing import Callable

from .recording import ExperimentArtifact, inspect_experiment_artifact


SUPPORTED_ARTIFACT_SUFFIXES = frozenset(
    {
        ".sxm",
        ".dat",
        ".3ds",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".bmp",
        ".svg",
    }
)


@dataclass(frozen=True)
class _FileFingerprint:
    size_bytes: int
    mtime_ns: int


@dataclass
class _Candidate:
    fingerprint: _FileFingerprint
    stable_polls: int


class SessionArtifactObserver:
    """Observe newly completed Nanonis/figure files without touching the instrument."""

    def __init__(
        self,
        session_path: str | Path,
        sink: Callable[[ExperimentArtifact], None],
        *,
        poll_interval_s: float = 0.5,
        stable_polls: int = 2,
        recursive: bool = False,
        monotonic_clock_ns: Callable[[], int] = time.perf_counter_ns,
        utc_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if poll_interval_s <= 0.0:
            raise ValueError("artifact poll_interval_s must be positive")
        if stable_polls < 2:
            raise ValueError("artifact stable_polls must be at least 2")
        self.session_path = Path(session_path).expanduser().resolve()
        self.sink = sink
        self.poll_interval_s = float(poll_interval_s)
        self.stable_polls = int(stable_polls)
        self.recursive = bool(recursive)
        self._monotonic_clock_ns = monotonic_clock_ns
        self._utc_clock_ns = utc_clock_ns
        self._stop = Event()
        self._thread: Thread | None = None
        self._error: BaseException | None = None
        self._baseline: dict[Path, _FileFingerprint] = {}
        self._emitted: dict[Path, _FileFingerprint] = {}
        self._candidates: dict[Path, _Candidate] = {}
        self._file_paths: dict[str, Path] = {}

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def error(self) -> BaseException | None:
        return self._error

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("session artifact observer failed") from self._error

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("session artifact observer has already been started")
        if not self.session_path.is_dir():
            raise FileNotFoundError(
                f"artifact session path is not a directory: {self.session_path}"
            )
        self._baseline = self._scan()
        self._thread = Thread(
            target=self._run,
            name="session-artifact-observer",
            daemon=True,
        )
        self._thread.start()

    def stop(
        self,
        *,
        raise_on_failure: bool = True,
        flush_pending: bool = True,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval_s * 4.0))
            if self._thread.is_alive():
                raise TimeoutError("session artifact observer did not stop")
        if flush_pending and self._error is None:
            self._flush_stable_candidates()
        if raise_on_failure:
            self.raise_if_failed()

    def _flush_stable_candidates(self) -> None:
        """Give just-finished files a bounded final stability window."""

        deadline = time.perf_counter() + self.poll_interval_s * (
            self.stable_polls + 1
        )
        while True:
            self._poll_once()
            if not self._candidates or time.perf_counter() >= deadline:
                return
            time.sleep(self.poll_interval_s)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._poll_once()
                self._stop.wait(self.poll_interval_s)
        except BaseException as exc:
            self._error = exc

    def _poll_once(self) -> None:
        current = self._scan()
        current_paths = set(current)
        for path in set(self._candidates) - current_paths:
            self._candidates.pop(path, None)

        for path, fingerprint in current.items():
            if self._emitted.get(path) == fingerprint:
                continue
            if self._baseline.get(path) == fingerprint:
                continue
            candidate = self._candidates.get(path)
            if candidate is None or candidate.fingerprint != fingerprint:
                self._candidates[path] = _Candidate(fingerprint, 1)
                continue
            candidate.stable_polls += 1
            if candidate.stable_polls < self.stable_polls:
                continue

            artifact = inspect_experiment_artifact(
                path,
                observed_monotonic_ns=self._monotonic_clock_ns(),
                observed_utc_ns=self._utc_clock_ns(),
                metadata={
                    "observer": "session_directory",
                    "association": (
                        "unresolved: no unique producer action was proven"
                    ),
                },
            )
            self.sink(artifact)
            self._emitted[path] = fingerprint
            self._baseline.pop(path, None)
            self._candidates.pop(path, None)

    def _scan(self) -> dict[Path, _FileFingerprint]:
        result: dict[Path, _FileFingerprint] = {}
        file_paths: dict[str, Path] = {}
        directories = [self.session_path]
        while directories:
            directory = directories.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if self.recursive:
                                    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                                    if not attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT:
                                        directories.append(Path(entry.path))
                                continue
                            if os.path.splitext(entry.name)[1].lower() not in SUPPORTED_ARTIFACT_SUFFIXES:
                                continue
                            if not entry.is_file():
                                continue
                            # Windows directory enumeration already contains
                            # file size/mtime. Path.stat()/resolve() per file
                            # otherwise opens thousands of handles every poll.
                            stat = entry.stat()
                            path = self._file_paths.get(entry.path)
                            if path is None:
                                path = Path(entry.path)
                            if entry.is_symlink():
                                path = path.resolve()
                        except FileNotFoundError:
                            continue
                        result[path] = _FileFingerprint(
                            size_bytes=int(stat.st_size), mtime_ns=int(stat.st_mtime_ns))
                        file_paths[entry.path] = path
            except FileNotFoundError:
                if directory == self.session_path:
                    raise
        self._file_paths = file_paths
        return result
