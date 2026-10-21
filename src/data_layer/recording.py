from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from queue import Full, Queue
import shutil
import sqlite3
from threading import Event, Thread
import time
from typing import Any
import uuid

from instrument_layer import InstrumentActionEvent


RECORDING_SCHEMA_VERSION = 5
MIN_RECORDING_FREE_BYTES = 1024**3


@dataclass(frozen=True)
class TelemetrySample:
    seq: int
    segment_id: int
    monotonic_ns: int
    utc_ns: int
    call_started_monotonic_ns: int
    call_finished_monotonic_ns: int
    current_a: float
    x_m: float
    y_m: float
    z_m: float
    quality_flags: tuple[str, ...] = ()
    scheduled_monotonic_ns: int | None = None
    lock_in_x_a: float | None = None
    lock_in_y_a: float | None = None


@dataclass(frozen=True)
class ExperimentMetadata:
    """User-supplied run context; sample identity is never inferred from signals."""

    sample_id: str | None = None
    description: str | None = None
    preparation: str | None = None
    operator: str | None = None
    tags: dict[str, str] | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "sample_id",
            "description",
            "preparation",
            "operator",
            "notes",
        ):
            value = getattr(self, field_name)
            if value is not None and len(value) > 4096:
                raise ValueError(f"experiment metadata {field_name} is too long")
        if self.tags is not None:
            if len(self.tags) > 128:
                raise ValueError("experiment metadata supports at most 128 tags")
            if any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or len(key) > 128
                or len(value) > 1024
                for key, value in self.tags.items()
            ):
                raise ValueError("experiment metadata tags must be bounded non-empty strings")


@dataclass(frozen=True)
class StorageEstimate:
    hours: float
    samples_per_s: float
    sample_count: int
    active_fraction: float
    numeric_payload_bytes: int
    telemetry_store_bytes: int
    nanonis_file_bytes: int
    combined_bytes: int


@dataclass(frozen=True)
class NanonisDataFileSummary:
    path: Path
    size_bytes: int
    sha256: str
    columns: tuple[str, ...]
    data_rows: int
    malformed_rows: int


class ArtifactKind(str, Enum):
    SCAN_RAW = "scan_raw"
    SPECTRUM_RAW = "spectrum_raw"
    GRID_SPECTROSCOPY_RAW = "grid_spectroscopy_raw"
    DATA_LOGGER_RAW = "data_logger_raw"
    NANONIS_DAT = "nanonis_dat"
    RENDERED_IMAGE = "rendered_image"
    UNKNOWN = "unknown"


class ArtifactLinkStatus(str, Enum):
    LINKED = "linked"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ExperimentArtifact:
    """Immutable file evidence observed during one experiment run."""

    artifact_id: str
    kind: ArtifactKind
    source_path: Path
    size_bytes: int
    sha256: str
    source_mtime_ns: int
    observed_monotonic_ns: int
    observed_utc_ns: int
    link_status: ArtifactLinkStatus
    producer_action_id: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id must not be empty")
        if self.size_bytes < 0:
            raise ValueError("artifact size must be non-negative")
        if (
            len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("artifact sha256 must be a lowercase hexadecimal digest")
        if min(
            self.source_mtime_ns,
            self.observed_monotonic_ns,
            self.observed_utc_ns,
        ) < 0:
            raise ValueError("artifact timestamps must be non-negative")
        if (
            self.link_status is ArtifactLinkStatus.LINKED
            and not self.producer_action_id
        ):
            raise ValueError("linked artifacts require producer_action_id")
        if (
            self.link_status is ArtifactLinkStatus.UNRESOLVED
            and self.producer_action_id is not None
        ):
            raise ValueError("unresolved artifacts must not claim a producer_action_id")


@dataclass(frozen=True)
class SavedPointsReconciliation:
    saved_points: int
    ingested_rows: int
    malformed_rows: int
    complete: bool
    difference: int
    reason: str | None


class RunStore:
    """Durable single-writer SQLite store for one active experiment run."""

    def __init__(self, run_id: str, run_dir: Path, connection: sqlite3.Connection) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.database_path = run_dir / "run.sqlite"
        self._connection = connection
        self._closed = False

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        signal_catalog: dict[str, int],
        requested_period_ns: int,
        run_id: str | None = None,
        now: datetime | None = None,
        experiment_metadata: ExperimentMetadata | None = None,
    ) -> "RunStore":
        now = now or datetime.now(timezone.utc)
        experiment_metadata = experiment_metadata or ExperimentMetadata()
        run_id = run_id or f"run-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        _validate_run_id(run_id)
        root_path = Path(root).expanduser()
        run_dir = root_path / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        database_path = run_dir / "run.sqlite"
        connection = sqlite3.connect(database_path, timeout=5.0, check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                started_utc_ns INTEGER NOT NULL,
                stopped_utc_ns INTEGER,
                status TEXT NOT NULL,
                requested_period_ns INTEGER NOT NULL,
                signal_catalog_json TEXT NOT NULL,
                experiment_metadata_json TEXT NOT NULL
            );
            CREATE TABLE signal_samples (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                seq INTEGER NOT NULL,
                segment_id INTEGER NOT NULL,
                monotonic_ns INTEGER NOT NULL,
                utc_ns INTEGER NOT NULL,
                call_started_monotonic_ns INTEGER NOT NULL,
                call_finished_monotonic_ns INTEGER NOT NULL,
                scheduled_monotonic_ns INTEGER NOT NULL,
                current_a REAL NOT NULL,
                x_m REAL NOT NULL,
                y_m REAL NOT NULL,
                z_m REAL NOT NULL,
                lock_in_x_a REAL,
                lock_in_y_a REAL,
                quality_flags_json TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            );
            CREATE TABLE events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                monotonic_ns INTEGER NOT NULL,
                utc_ns INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE action_events (
                action_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                action_id TEXT NOT NULL,
                monotonic_ns INTEGER NOT NULL,
                utc_ns INTEGER NOT NULL,
                action_name TEXT NOT NULL,
                phase TEXT NOT NULL,
                actor TEXT NOT NULL,
                intent TEXT NOT NULL,
                source TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                result_json TEXT,
                error_type TEXT,
                error_message TEXT
            );
            CREATE INDEX action_events_action_id
                ON action_events(run_id, action_id, action_event_id);
            CREATE INDEX action_events_utc_ns
                ON action_events(run_id, utc_ns, action_event_id);
            CREATE TABLE ingested_files (
                file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                source_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                data_rows INTEGER NOT NULL,
                malformed_rows INTEGER NOT NULL,
                saved_points INTEGER,
                reconciliation_complete INTEGER,
                UNIQUE (run_id, source_path, sha256)
            );
            CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                producer_action_id TEXT,
                artifact_kind TEXT NOT NULL,
                link_status TEXT NOT NULL,
                source_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                observed_monotonic_ns INTEGER NOT NULL,
                observed_utc_ns INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE (run_id, source_path, sha256)
            );
            CREATE INDEX artifacts_action_id
                ON artifacts(run_id, producer_action_id, artifact_id);
            CREATE INDEX artifacts_observed_utc_ns
                ON artifacts(run_id, observed_utc_ns, artifact_id);
            """
        )
        started_utc_ns = int(now.timestamp() * 1_000_000_000)
        connection.execute(
            """
            INSERT INTO runs (
                run_id, schema_version, started_utc_ns, status,
                requested_period_ns, signal_catalog_json, experiment_metadata_json
            ) VALUES (?, ?, ?, 'recording', ?, ?, ?)
            """,
            (
                run_id,
                RECORDING_SCHEMA_VERSION,
                started_utc_ns,
                int(requested_period_ns),
                _json(signal_catalog),
                _json(asdict(experiment_metadata)),
            ),
        )
        connection.commit()
        store = cls(run_id, run_dir, connection)
        store._write_manifest(
            {
                "schema_version": RECORDING_SCHEMA_VERSION,
                "run_id": run_id,
                "started_utc_ns": started_utc_ns,
                "status": "recording",
                "requested_period_ns": int(requested_period_ns),
                "signal_catalog": signal_catalog,
                "experiment_metadata": asdict(experiment_metadata),
                "database": "run.sqlite",
            }
        )
        return store

    def append_sample(self, sample: TelemetrySample) -> None:
        self.append_samples((sample,))

    @classmethod
    def open_artifact_archive(cls, database_path: str | Path) -> "RunStore":
        """Reopen only a completed offline artifact archive, never a recorder."""
        path = Path(database_path).resolve()
        if path.name != "run.sqlite":
            raise ValueError("archive database must be run.sqlite")
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5.0)
        try:
            rows = connection.execute("SELECT run_id,status,requested_period_ns,signal_catalog_json,experiment_metadata_json FROM runs").fetchall()
            if len(rows) != 1:
                raise ValueError("expected one archive experiment")
            run_id, status, period, signals, metadata = rows[0]
            tags = json.loads(metadata).get("tags") or {}
            samples = connection.execute("SELECT COUNT(*) FROM signal_samples").fetchone()[0]
            if (tags.get("record_kind") != "agent_experiment_archive" or
                    status not in {"complete", "failed"} or period != 0 or json.loads(signals) or samples):
                raise ValueError("not a closed offline artifact archive; recorder databases are not writable here")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            return cls(run_id, path.parent, connection)
        except BaseException:
            connection.close()
            raise

    def artifact_index(self) -> list[dict[str, Any]]:
        self._ensure_open()
        cursor = self._connection.execute(
            "SELECT artifact_id,artifact_kind,source_path,sha256,size_bytes,metadata_json FROM artifacts ORDER BY observed_utc_ns,artifact_id")
        return [dict(artifact_id=r[0], kind=r[1], path=r[2], sha256=r[3], size_bytes=r[4],
                     metadata=json.loads(r[5])) for r in cursor]

    def append_samples(self, samples: tuple[TelemetrySample, ...]) -> None:
        self._ensure_open()
        if not samples:
            return
        self._connection.executemany(
            """
            INSERT INTO signal_samples (
                run_id, seq, segment_id, monotonic_ns, utc_ns,
                call_started_monotonic_ns, call_finished_monotonic_ns,
                scheduled_monotonic_ns, current_a, x_m, y_m, z_m,
                quality_flags_json, lock_in_x_a, lock_in_y_a
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    self.run_id,
                    sample.seq,
                    sample.segment_id,
                    sample.monotonic_ns,
                    sample.utc_ns,
                    sample.call_started_monotonic_ns,
                    sample.call_finished_monotonic_ns,
                    (
                        sample.monotonic_ns
                        if sample.scheduled_monotonic_ns is None
                        else sample.scheduled_monotonic_ns
                    ),
                    sample.current_a,
                    sample.x_m,
                    sample.y_m,
                    sample.z_m,
                    _json(sample.quality_flags),
                    sample.lock_in_x_a,
                    sample.lock_in_y_a,
                )
                for sample in samples
            ),
        )
        self._connection.commit()

    def checkpoint(self) -> None:
        self._ensure_open()
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        monotonic_ns: int,
        utc_ns: int,
    ) -> None:
        self._ensure_open()
        if not event_type:
            raise ValueError("event_type must not be empty")
        self._connection.execute(
            """
            INSERT INTO events (run_id, monotonic_ns, utc_ns, event_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (self.run_id, int(monotonic_ns), int(utc_ns), event_type, _json(payload)),
        )
        self._connection.commit()

    def append_action_event(self, event: InstrumentActionEvent) -> None:
        self._ensure_open()
        _insert_action_event(self._connection, self.run_id, event)
        self._connection.commit()

    def append_artifact(self, artifact: ExperimentArtifact) -> None:
        """Persist artifact metadata and its typed event in one transaction."""

        self._ensure_open()
        self._insert_artifact(artifact)
        self._connection.commit()

    def _insert_artifact(self, artifact: ExperimentArtifact) -> None:
        self._connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, run_id, producer_action_id, artifact_kind,
                link_status, source_path, size_bytes, sha256,
                source_mtime_ns, observed_monotonic_ns, observed_utc_ns,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.artifact_id,
                self.run_id,
                artifact.producer_action_id,
                artifact.kind.value,
                artifact.link_status.value,
                str(artifact.source_path),
                int(artifact.size_bytes),
                artifact.sha256,
                int(artifact.source_mtime_ns),
                int(artifact.observed_monotonic_ns),
                int(artifact.observed_utc_ns),
                _json(artifact.metadata or {}),
            ),
        )
        self._connection.execute(
            """
            INSERT INTO events (
                run_id, monotonic_ns, utc_ns, event_type, payload_json
            ) VALUES (?, ?, ?, 'artifact_saved', ?)
            """,
            (
                self.run_id,
                int(artifact.observed_monotonic_ns),
                int(artifact.observed_utc_ns),
                _json(
                    {
                        "artifact_id": artifact.artifact_id,
                        "artifact_kind": artifact.kind.value,
                        "link_status": artifact.link_status.value,
                        "producer_action_id": artifact.producer_action_id,
                        "source_path": str(artifact.source_path),
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "source_mtime_ns": artifact.source_mtime_ns,
                        "metadata": artifact.metadata or {},
                    }
                ),
            ),
        )

    def ingest_nanonis_file(
        self,
        summary: NanonisDataFileSummary,
        *,
        saved_points: int | None = None,
    ) -> SavedPointsReconciliation | None:
        self._ensure_open()
        reconciliation = (
            reconcile_saved_points(saved_points, summary) if saved_points is not None else None
        )
        artifact = experiment_artifact_from_summary(
            summary,
            observed_monotonic_ns=time.perf_counter_ns(),
            observed_utc_ns=time.time_ns(),
            metadata={
                "ingest": "nanonis_tab_file",
                "saved_points": saved_points,
                "reconciliation_complete": (
                    None if reconciliation is None else reconciliation.complete
                ),
            },
        )
        self._insert_artifact(artifact)
        self._connection.execute(
            """
            INSERT INTO ingested_files (
                run_id, source_path, size_bytes, sha256, columns_json,
                data_rows, malformed_rows, saved_points, reconciliation_complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                str(summary.path),
                summary.size_bytes,
                summary.sha256,
                _json(summary.columns),
                summary.data_rows,
                summary.malformed_rows,
                saved_points,
                None if reconciliation is None else int(reconciliation.complete),
            ),
        )
        if reconciliation is not None:
            self._connection.execute(
                """
                INSERT INTO events (
                    run_id, monotonic_ns, utc_ns, event_type, payload_json
                ) VALUES (?, ?, ?, 'data_logger_reconciliation', ?)
                """,
                (
                    self.run_id,
                    artifact.observed_monotonic_ns,
                    artifact.observed_utc_ns,
                    _json(
                        {
                            "source_path": str(summary.path),
                            "saved_points": reconciliation.saved_points,
                            "ingested_rows": reconciliation.ingested_rows,
                            "malformed_rows": reconciliation.malformed_rows,
                            "difference": reconciliation.difference,
                            "complete": reconciliation.complete,
                            "reason": reconciliation.reason,
                        }
                    ),
                ),
            )
        self._connection.commit()
        return reconciliation

    def sample_count(self) -> int:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT COUNT(*) FROM signal_samples WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        return int(row[0])

    def action_event_count(self) -> int:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT COUNT(*) FROM action_events WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        return int(row[0])

    def artifact_count(self) -> int:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        return int(row[0])

    def finalize(self, status: str, *, stopped_utc_ns: int) -> None:
        self._ensure_open()
        if status not in {
            "complete",
            "stopped",
            "storage_quota_reached",
            "recording_compromised",
            "failed",
        }:
            raise ValueError(f"unsupported terminal run status: {status}")
        self._connection.execute(
            "UPDATE runs SET stopped_utc_ns = ?, status = ? WHERE run_id = ?",
            (int(stopped_utc_ns), status, self.run_id),
        )
        self._connection.commit()
        row = self._connection.execute(
            """
            SELECT started_utc_ns, requested_period_ns, signal_catalog_json,
                   experiment_metadata_json, schema_version
            FROM runs WHERE run_id = ?
            """,
            (self.run_id,),
        ).fetchone()
        self._write_manifest(
            {
                "schema_version": int(row[4]),
                "run_id": self.run_id,
                "started_utc_ns": int(row[0]),
                "stopped_utc_ns": int(stopped_utc_ns),
                "status": status,
                "requested_period_ns": int(row[1]),
                "signal_catalog": json.loads(row[2]),
                "sample_count": self.sample_count(),
                "action_event_count": self.action_event_count(),
                "artifact_count": self.artifact_count(),
                "experiment_metadata": json.loads(row[3]),
                "database": "run.sqlite",
            }
        )

    def close(self) -> None:
        if self._closed:
            return
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.close()
        self._closed = True

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _write_manifest(self, payload: dict[str, Any]) -> None:
        if (payload.get("experiment_metadata", {}).get("tags") or {}).get("record_kind") == "agent_experiment_archive":
            payload["artifacts"] = self.artifact_index()
            payload["artifact_links"] = [json.loads(r[0]) for r in self._connection.execute(
                "SELECT payload_json FROM events WHERE event_type='experiment_artifact_linked' ORDER BY event_id")]
        path = self.run_dir / "manifest.json"
        temporary = self.run_dir / "manifest.json.tmp"
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("RunStore is closed")


class DurableRunWriter:
    """Single asynchronous writer with ordered sample transactions."""

    def __init__(
        self,
        store: RunStore,
        *,
        queue_capacity: int = 5000,
        sample_batch_size: int = 10,
    ) -> None:
        if queue_capacity < 10:
            raise ValueError("queue_capacity must be at least 10")
        if sample_batch_size < 1:
            raise ValueError("sample_batch_size must be positive")
        self.store = store
        self.queue_capacity = int(queue_capacity)
        self.sample_batch_size = int(sample_batch_size)
        self.high_watermark = max(1, int(queue_capacity * 0.8))
        self.maximum_queue_depth = 0
        self.samples_committed = 0
        self._queue: Queue[tuple[str, Any] | None] = Queue(maxsize=queue_capacity)
        self._error: BaseException | None = None
        self._thread = Thread(target=self._run, name=f"run-writer-{store.run_id}", daemon=True)
        self._thread.start()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def at_high_watermark(self) -> bool:
        return self.queue_depth >= self.high_watermark

    @property
    def failed(self) -> bool:
        return self._error is not None

    def enqueue_sample(self, sample: TelemetrySample) -> None:
        self._enqueue(("sample", sample))

    def enqueue_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        monotonic_ns: int,
        utc_ns: int,
    ) -> None:
        self._enqueue(("event", (event_type, payload, int(monotonic_ns), int(utc_ns))))

    def enqueue_action_event(self, event: InstrumentActionEvent) -> None:
        committed = Event()
        self._enqueue(("action_event", (event, committed)))
        if not committed.wait(timeout=5.0):
            raise TimeoutError(
                "action event was not durably committed within 5 seconds"
            )
        self.raise_if_failed()

    def enqueue_artifact(self, artifact: ExperimentArtifact) -> None:
        committed = Event()
        self._enqueue(("artifact", (artifact, committed)))
        if not committed.wait(timeout=30.0):
            raise TimeoutError(
                "artifact event was not durably committed within 30 seconds"
            )
        self.raise_if_failed()

    def enqueue_nanonis_file(
        self,
        summary: NanonisDataFileSummary,
        *,
        saved_points: int,
    ) -> SavedPointsReconciliation:
        committed = Event()
        result: list[SavedPointsReconciliation | None] = []
        self._enqueue(
            (
                "nanonis_file",
                (summary, int(saved_points), committed, result),
            )
        )
        if not committed.wait(timeout=30.0):
            raise TimeoutError(
                "Nanonis file ingest was not durably committed within "
                "30 seconds"
            )
        self.raise_if_failed()
        reconciliation = result[0] if result else None
        if reconciliation is None:
            raise RuntimeError(
                "Nanonis file ingest completed without reconciliation"
            )
        return reconciliation

    def enqueue_checkpoint(self) -> None:
        self._enqueue(("checkpoint", None))

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("durable run writer failed") from self._error

    def close(self) -> None:
        self.raise_if_failed()
        if self._thread.is_alive():
            self._queue.put(None, timeout=5.0)
            self._thread.join(timeout=30.0)
        if self._thread.is_alive():
            raise TimeoutError("durable run writer did not stop within 30 seconds")
        self.raise_if_failed()

    def _enqueue(self, item: tuple[str, Any]) -> None:
        self.raise_if_failed()
        try:
            self._queue.put(item, timeout=1.0)
        except Full as exc:
            raise BufferError("durable run writer queue is full; no item was accepted") from exc
        self.maximum_queue_depth = max(self.maximum_queue_depth, self.queue_depth)

    def _run(self) -> None:
        deferred: tuple[str, Any] | None | object = _NO_DEFERRED_ITEM
        try:
            while True:
                if deferred is _NO_DEFERRED_ITEM:
                    item = self._queue.get()
                else:
                    item = deferred
                    deferred = _NO_DEFERRED_ITEM
                if item is not None and item[0] == "sample":
                    samples = [item[1]]
                    consumed = 1
                    while len(samples) < self.sample_batch_size:
                        following = self._queue.get()
                        if (
                            following is not None
                            and following[0] == "sample"
                        ):
                            samples.append(following[1])
                            consumed += 1
                            continue
                        deferred = following
                        break
                    try:
                        self.store.append_samples(tuple(samples))
                        self.samples_committed += len(samples)
                    finally:
                        for _ in range(consumed):
                            self._queue.task_done()
                    continue
                try:
                    if item is None:
                        return
                    kind, payload = item
                    if kind == "sample":
                        self.store.append_sample(payload)
                    elif kind == "event":
                        event_type, event_payload, monotonic_ns, utc_ns = payload
                        self.store.append_event(
                            event_type,
                            event_payload,
                            monotonic_ns=monotonic_ns,
                            utc_ns=utc_ns,
                        )
                    elif kind == "action_event":
                        action_event, committed = payload
                        try:
                            self.store.append_action_event(action_event)
                        finally:
                            committed.set()
                    elif kind == "artifact":
                        artifact, committed = payload
                        try:
                            self.store.append_artifact(artifact)
                        finally:
                            committed.set()
                    elif kind == "nanonis_file":
                        summary, saved_points, committed, result = payload
                        try:
                            result.append(
                                self.store.ingest_nanonis_file(
                                    summary,
                                    saved_points=saved_points,
                                )
                            )
                        finally:
                            committed.set()
                    elif kind == "checkpoint":
                        self.store.checkpoint()
                    else:
                        raise ValueError(f"unknown writer item kind: {kind}")
                finally:
                    self._queue.task_done()
        except BaseException as exc:
            self._error = exc


_NO_DEFERRED_ITEM = object()


class RunStoreActionEventSink:
    """Lossless adapter from instrument/runtime typed events to one run writer."""

    def __init__(self, writer: DurableRunWriter) -> None:
        self.writer = writer

    def __call__(self, event: InstrumentActionEvent) -> None:
        self.writer.enqueue_action_event(event)


class ExistingRunActionEventSink:
    """Synchronously append control events to a recorder owned by another process."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        if not self.database_path.is_file():
            raise FileNotFoundError(self.database_path)
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            rows = connection.execute("SELECT run_id, status FROM runs").fetchall()
        finally:
            connection.close()
        if len(rows) != 1:
            raise ValueError("an existing run database must contain exactly one run")
        self.run_id = str(rows[0][0])
        if str(rows[0][1]) != "recording":
            raise RuntimeError(
                f"run {self.run_id} is not active (status={rows[0][1]!r})"
            )

    def __call__(self, event: InstrumentActionEvent) -> None:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            status = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()
            if status is None or str(status[0]) != "recording":
                raise RuntimeError(f"run {self.run_id} is no longer recording")
            _insert_action_event(connection, self.run_id, event)
            connection.commit()
        finally:
            connection.close()


def _insert_action_event(
    connection: sqlite3.Connection,
    run_id: str,
    event: InstrumentActionEvent,
) -> None:
    connection.execute(
        """
        INSERT INTO action_events (
            run_id, action_id, monotonic_ns, utc_ns, action_name, phase,
            actor, intent, source, arguments_json, result_json,
            error_type, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            event.action_id,
            int(event.monotonic_ns),
            int(event.utc_ns),
            event.action_name.value,
            event.phase.value,
            event.actor.value,
            event.intent.value,
            event.source,
            _json(asdict(event.arguments)),
            None if event.result is None else _json(asdict(event.result)),
            event.error_type,
            event.error_message,
        ),
    )


def latest_completed_action_id(
    database_path: str | Path,
    *,
    run_id: str,
    action_names: tuple[str, ...],
    before_utc_ns: int,
) -> str | None:
    """Find the latest successful typed action preceding a saved artifact."""

    if not action_names:
        return None
    path = Path(database_path).resolve()
    placeholders = ", ".join("?" for _ in action_names)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        row = connection.execute(
            f"""
            SELECT action_id
            FROM action_events
            WHERE run_id = ?
              AND action_name IN ({placeholders})
              AND phase = 'after'
              AND error_type IS NULL
              AND utc_ns <= ?
            ORDER BY utc_ns DESC, action_event_id DESC
            LIMIT 1
            """,
            (run_id, *action_names, int(before_utc_ns)),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def estimate_recording_storage(
    *,
    hours: float,
    samples_per_s: float = 50.0,
    telemetry_bytes_per_sample: int = 256,
    nanonis_bytes_per_sample: int = 256,
    active_fraction: float = 1.0,
) -> StorageEstimate:
    if hours < 0.0 or samples_per_s <= 0.0:
        raise ValueError("hours must be non-negative and samples_per_s must be positive")
    if not 0.0 <= active_fraction <= 1.0:
        raise ValueError("active_fraction must be between zero and one")
    samples = int(round(hours * 3600.0 * samples_per_s * active_fraction))
    numeric_payload = samples * 40  # UTC/sequence plus four float64 signal values.
    telemetry = samples * int(telemetry_bytes_per_sample)
    nanonis = samples * int(nanonis_bytes_per_sample)
    return StorageEstimate(
        hours=hours,
        samples_per_s=samples_per_s,
        sample_count=samples,
        active_fraction=active_fraction,
        numeric_payload_bytes=numeric_payload,
        telemetry_store_bytes=telemetry,
        nanonis_file_bytes=nanonis,
        combined_bytes=telemetry + nanonis,
    )


def recording_tree_size_bytes(root: str | Path) -> int:
    """Return the bytes currently owned by the configured recording tree."""

    path = Path(root).expanduser()
    if not path.exists():
        return 0
    if path.is_file():
        return int(path.stat().st_size)
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += int(item.stat().st_size)
        except FileNotFoundError:
            # A live run may atomically rename or remove a temporary file while
            # the quota scan is in progress.
            continue
    return total


def recording_root_free_bytes(root: str | Path) -> int:
    """Return physical free bytes on the volume that owns the recording root."""

    existing = Path(root).expanduser().resolve()
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    return int(shutil.disk_usage(existing).free)


def inspect_nanonis_tab_file(path: str | Path) -> NanonisDataFileSummary:
    source = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    columns: tuple[str, ...] = ()
    data_rows = 0
    malformed_rows = 0
    in_data = False
    waiting_for_header = False
    with source.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if not in_data:
                if line.strip() == "[DATA]":
                    in_data = True
                    waiting_for_header = True
                continue
            if not line.strip():
                continue
            fields = tuple(part.strip() for part in line.split("\t"))
            if waiting_for_header:
                columns = fields
                waiting_for_header = False
                continue
            data_rows += 1
            if len(fields) != len(columns):
                malformed_rows += 1

    if not in_data:
        raise ValueError(f"{source} does not contain a [DATA] section")
    if not columns:
        raise ValueError(f"{source} does not contain a data header after [DATA]")
    stat = source.stat()
    return NanonisDataFileSummary(
        path=source,
        size_bytes=stat.st_size,
        sha256=digest.hexdigest(),
        columns=columns,
        data_rows=data_rows,
        malformed_rows=malformed_rows,
    )


@contextmanager
def open_run_readonly(database_path: str | Path):
    """Read a live WAL normally; allow immutable reads only for sealed, checkpointed runs.

    Windows cannot open a WAL-mode database in a read-only directory after its
    sidecars have been removed. A matching terminal manifest and no WAL are
    required for that fallback; a stale 'recording' run never qualifies.
    """
    path = Path(database_path).resolve()
    connection = None
    try:
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            connection.execute("SELECT status FROM runs LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            if connection is not None:
                connection.close()
                connection = None
            if not path.is_file() or not (path.parent / "manifest.json").is_file():
                raise
            manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
            if (manifest.get("status") not in {"complete", "stopped", "recording_compromised", "failed", "storage_quota_reached"}
                    or not manifest.get("stopped_utc_ns") or Path(str(path) + "-wal").exists()):
                raise ValueError("cannot read live/unfinalized database without its WAL")
            connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            row = connection.execute("SELECT run_id,status,stopped_utc_ns FROM runs").fetchall()
            if row != [(manifest["run_id"], manifest["status"], manifest["stopped_utc_ns"])] or Path(str(path) + "-wal").exists():
                raise ValueError("sealed run does not match its manifest")
        yield connection
    finally:
        if connection is not None:
            connection.close()


def read_recording_snapshot(database_path: str | Path) -> dict:
    """Consistent bounded read model for summaries; no instrument or raw file reads."""
    with open_run_readonly(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        runs = connection.execute("SELECT * FROM runs").fetchall()
        if len(runs) != 1:
            raise ValueError("expected one run in database")
        run = dict(runs[0])
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("signal_samples", "events", "action_events", "artifacts")}
        watermarks = {table: connection.execute(f"SELECT MAX({key}) FROM {table}").fetchone()[0]
                      for table, key in (("signal_samples", "seq"), ("events", "event_id"), ("action_events", "action_event_id"))}
        actions = [dict(row) for row in connection.execute("SELECT * FROM action_events ORDER BY monotonic_ns,action_event_id LIMIT 100000")]
        events = [dict(row) for row in connection.execute(
            "SELECT * FROM events WHERE event_type NOT IN ('telemetry_gap','tip_proximity_state_changed') ORDER BY monotonic_ns,event_id LIMIT 10000")]
        event_counts = dict(connection.execute("SELECT event_type,COUNT(*) FROM events GROUP BY event_type"))
        artifacts = [dict(row) for row in connection.execute("SELECT * FROM artifacts ORDER BY observed_monotonic_ns LIMIT 1000")]
        sample_stats = dict(connection.execute(
            "SELECT COUNT(*) AS count,MIN(utc_ns) AS first_utc_ns,MAX(utc_ns) AS last_utc_ns,"
            "MIN(monotonic_ns) AS first_monotonic_ns,MAX(monotonic_ns) AS last_monotonic_ns FROM signal_samples").fetchone())
        intervals = dict(connection.execute(
            "SELECT MIN(dt) AS minimum_interval_ns,MAX(dt) AS maximum_interval_ns,AVG(dt) AS mean_interval_ns,"
            "COUNT(dt) AS interval_count,COALESCE(SUM(dt),0) AS sampled_duration_ns FROM "
            "(SELECT monotonic_ns-LAG(monotonic_ns) OVER (PARTITION BY segment_id ORDER BY seq) AS dt FROM signal_samples)").fetchone())
        missed = connection.execute(
            "SELECT COALESCE(SUM(json_extract(payload_json,'$.estimated_missed_deadlines')),0) FROM events WHERE event_type='telemetry_gap'").fetchone()[0]
        for action in actions:
            for column in ("arguments_json", "result_json"):
                action[column.removesuffix("_json")] = json.loads(action.pop(column) or "null")
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return {"database_path": str(Path(database_path).resolve()), "run": run, "counts": counts,
                "watermarks": watermarks, "actions": actions, "events": events, "event_counts": event_counts,
                "artifacts": artifacts, "sample_stats": sample_stats | intervals | {"estimated_missed_deadlines": missed},
                "truncated": {"actions": counts["action_events"] > len(actions), "artifacts": counts["artifacts"] > len(artifacts),
                              "events": sum(n for k,n in event_counts.items() if k not in {"telemetry_gap","tip_proximity_state_changed"}) > len(events)}}


def read_run_event_page(database_path: str | Path, *, after_event_id: int = 0, limit: int = 100) -> dict:
    """Read one consistent event page without creating or modifying a run database."""
    if type(after_event_id) is not int or after_event_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid run event pagination")
    with open_run_readonly(database_path) as connection:
        connection.execute("BEGIN")
        runs = connection.execute("SELECT run_id, status FROM runs").fetchall()
        if len(runs) != 1:
            raise ValueError("expected one run in database")
        rows = connection.execute(
            "SELECT event_id, event_type, utc_ns, payload_json FROM events WHERE event_id>? ORDER BY event_id LIMIT ?",
            (after_event_id, limit),
        ).fetchall()
        return {"run_id": runs[0][0], "status": runs[0][1], "events": [
            {"event_id": row[0], "event_type": row[1], "utc_ns": row[2], "payload": json.loads(row[3])} for row in rows
        ], "next_event_id": rows[-1][0] if rows else after_event_id}


def inspect_experiment_artifact(
    path: str | Path,
    *,
    observed_monotonic_ns: int,
    observed_utc_ns: int,
    producer_action_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    artifact_id: str | None = None,
    read_content_metadata: bool = False,
) -> ExperimentArtifact:
    source = Path(path).expanduser().resolve()
    stat_before = source.stat()
    digest = hashlib.sha256()
    header = bytearray()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            if len(header) < 64 * 1024:
                header.extend(block[: 64 * 1024 - len(header)])
    stat_after = source.stat()
    if (
        stat_before.st_size != stat_after.st_size
        or stat_before.st_mtime_ns != stat_after.st_mtime_ns
    ):
        raise RuntimeError(f"artifact changed while hashing: {source}")
    link_status = (
        ArtifactLinkStatus.LINKED
        if producer_action_id is not None
        else ArtifactLinkStatus.UNRESOLVED
    )
    if read_content_metadata:
        from .nanonis_offline import read_nanonis_dat, read_nanonis_sxm
        content = {}
        try:
            if source.suffix.lower() == ".dat":
                saved = read_nanonis_dat(source)
                content = {"header": dict(saved.header), "columns": list(saved.columns),
                           "points": int(saved.data.shape[0]),
                           "data_sha256": hashlib.sha256(saved.data.astype("<f8").tobytes()).hexdigest()}
            elif source.suffix.lower() == ".sxm":
                saved = read_nanonis_sxm(source)
                content = {"acquisition_date": saved.recorded_date, "acquisition_time": saved.recorded_time,
                           "width_m": saved.range_x_m, "height_m": saved.range_y_m,
                           "center_x_m": saved.offset_x_m, "center_y_m": saved.offset_y_m,
                           "bias_v": saved.bias_v, "setpoint_a": saved.z_setpoint_a,
                           "rows": saved.rows, "columns": saved.columns}
        except (ValueError, UnicodeError) as error:
            # Unknown/incomplete file formats are still evidence worth preserving.
            metadata = dict(metadata or {}, content_read_error=f"{type(error).__name__}: {error}")
        if content:
            if saved.sha256 != digest.hexdigest():
                raise RuntimeError(f"artifact changed while reading metadata: {source}")
            metadata = dict(metadata or {}, content=content)
    return ExperimentArtifact(
        artifact_id=artifact_id or uuid.uuid4().hex,
        kind=classify_artifact(source, bytes(header)),
        source_path=source,
        size_bytes=int(stat_after.st_size),
        sha256=digest.hexdigest(),
        source_mtime_ns=int(stat_after.st_mtime_ns),
        observed_monotonic_ns=int(observed_monotonic_ns),
        observed_utc_ns=int(observed_utc_ns),
        link_status=link_status,
        producer_action_id=producer_action_id,
        metadata=metadata,
    )


def experiment_artifact_from_summary(
    summary: NanonisDataFileSummary,
    *,
    observed_monotonic_ns: int,
    observed_utc_ns: int,
    producer_action_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    artifact_id: str | None = None,
) -> ExperimentArtifact:
    source = summary.path.expanduser().resolve()
    with source.open("rb") as handle:
        header = handle.read(64 * 1024)
    return ExperimentArtifact(
        artifact_id=artifact_id or uuid.uuid4().hex,
        kind=classify_artifact(source, header),
        source_path=source,
        size_bytes=int(summary.size_bytes),
        sha256=summary.sha256,
        source_mtime_ns=int(source.stat().st_mtime_ns),
        observed_monotonic_ns=int(observed_monotonic_ns),
        observed_utc_ns=int(observed_utc_ns),
        link_status=(
            ArtifactLinkStatus.LINKED
            if producer_action_id is not None
            else ArtifactLinkStatus.UNRESOLVED
        ),
        producer_action_id=producer_action_id,
        metadata=metadata,
    )


def classify_artifact(path: str | Path, header: bytes = b"") -> ArtifactKind:
    suffix = Path(path).suffix.lower()
    if suffix == ".sxm":
        return ArtifactKind.SCAN_RAW
    if suffix == ".3ds":
        return ArtifactKind.GRID_SPECTROSCOPY_RAW
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".svg"}:
        return ArtifactKind.RENDERED_IMAGE
    if suffix == ".dat":
        normalized = header.decode(
            "utf-8-sig",
            errors="replace",
        ).lower()
        if "bias spectr" in normalized or "spectroscopy" in normalized:
            return ArtifactKind.SPECTRUM_RAW
        if "data logger" in normalized:
            return ArtifactKind.DATA_LOGGER_RAW
        return ArtifactKind.NANONIS_DAT
    return ArtifactKind.UNKNOWN


def reconcile_saved_points(
    saved_points: int,
    summary: NanonisDataFileSummary,
) -> SavedPointsReconciliation:
    if saved_points < 0:
        raise ValueError("saved_points must be non-negative")
    complete = saved_points == summary.data_rows and summary.malformed_rows == 0
    if summary.malformed_rows:
        reason = "malformed_rows"
    elif saved_points != summary.data_rows:
        reason = "saved_points_mismatch"
    else:
        reason = None
    return SavedPointsReconciliation(
        saved_points=int(saved_points),
        ingested_rows=summary.data_rows,
        malformed_rows=summary.malformed_rows,
        complete=complete,
        difference=summary.data_rows - int(saved_points),
        reason=reason,
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_run_id(run_id: str) -> None:
    if not run_id or len(run_id) > 128:
        raise ValueError("run_id must contain 1-128 characters")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in run_id):
        raise ValueError("run_id contains unsafe path characters")
