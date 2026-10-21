"""One persistent agent experiment, using the existing RunStore and file readers.

No instrument connection, recorder attachment, inferred sample identity or new DB schema.
"""
from dataclasses import replace
from pathlib import Path
import re
import shutil
import time
import uuid

from data_layer.recording import ExperimentMetadata, RunStore, inspect_experiment_artifact


def create_experiment_archive(root, *, experiment_id, sample_id, sample_description,
                              identity_source, purpose):
    for name, value in dict(experiment_id=experiment_id, sample_id=sample_id,
            sample_description=sample_description, identity_source=identity_source, purpose=purpose).items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"explicit {name} is required; do not infer it from a directory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", experiment_id):
        raise ValueError("experiment_id must be a stable simple identifier")
    if any(Path(root).glob(f"*/*/*/{experiment_id}/run.sqlite")):
        raise FileExistsError("experiment ID already exists; append using its --database")
    store = RunStore.create(root, run_id=experiment_id, signal_catalog={}, requested_period_ns=0,
        experiment_metadata=ExperimentMetadata(sample_id=sample_id, description=sample_description,
            notes=purpose, tags={"record_kind":"agent_experiment_archive", "sample_identity_source":identity_source}))
    try:
        store.finalize("complete", stopped_utc_ns=time.time_ns())
        return {"experiment_id":store.run_id, "database_path":str(store.database_path.resolve()),
                "manifest_path":str((store.run_dir/'manifest.json').resolve())}
    finally:
        store.close()


def register_experiment_artifacts(database_path, artifacts, *, tip_state_id, purpose,
                                  parent_artifact_ids=()):
    """Copy each distinct byte sequence once; append explicit scientific context.

    artifacts contains (role, source_path). Repeated registration reuses content,
    while its link event preserves the role, tip state, parentage and original name.
    """
    if any(not isinstance(v, str) or not v.strip() for v in (tip_state_id, purpose)):
        raise ValueError("explicit tip_state_id and purpose are required")
    prepared = []
    for role, source in artifacts:
        if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", role):
            raise ValueError("artifact role must be a descriptive lowercase identifier")
        source = Path(source).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        prepared.append((role, source))
    with RunStore.open_artifact_archive(database_path) as store:
        existing = {a['sha256']:a for a in store.artifact_index()}
        known_ids = {a['artifact_id'] for a in existing.values()}
        if not set(parent_artifact_ids) <= known_ids:
            raise ValueError("parent artifacts must already be registered in this experiment")
        objects = store.run_dir / 'objects'
        objects.mkdir(exist_ok=True)
        links = []
        try:
            for role, source in prepared:
                artifact = inspect_experiment_artifact(source, observed_monotonic_ns=time.perf_counter_ns(),
                    observed_utc_ns=time.time_ns(), read_content_metadata=True,
                    metadata={"original_path":str(source), "original_name":source.name,
                              "sample_identity":"explicit experiment metadata, not filename or parent directory"})
                previous = existing.get(artifact.sha256)
                if previous:
                    destination = Path(previous['path'])
                    verified = inspect_experiment_artifact(destination,
                        observed_monotonic_ns=time.perf_counter_ns(), observed_utc_ns=time.time_ns())
                    if verified.sha256 != artifact.sha256:
                        raise RuntimeError("registered object changed; refusing to hide archive corruption")
                    artifact_id = previous['artifact_id']
                else:
                    # File type is metadata; object identity depends only on bytes.
                    destination = objects / artifact.sha256
                    if destination.exists():
                        verified = inspect_experiment_artifact(destination,
                            observed_monotonic_ns=time.perf_counter_ns(), observed_utc_ns=time.time_ns())
                        if verified.sha256 != artifact.sha256:
                            raise RuntimeError("existing object does not match its content hash")
                    else:
                        temporary = objects / (uuid.uuid4().hex + '.pending')
                        try:
                            shutil.copyfile(source, temporary)
                            verified = inspect_experiment_artifact(temporary,
                                observed_monotonic_ns=time.perf_counter_ns(), observed_utc_ns=time.time_ns())
                            if verified.sha256 != artifact.sha256:
                                raise RuntimeError("source changed while copying; no artifact registered")
                            temporary.rename(destination)
                        finally:
                            temporary.unlink(missing_ok=True)
                    artifact_id = artifact.artifact_id
                    store.append_artifact(replace(artifact, source_path=destination.resolve()))
                    existing[artifact.sha256] = {'artifact_id':artifact_id, 'path':str(destination)}
                link = {"artifact_id":artifact_id, "sha256":artifact.sha256, "role":role,
                        "tip_state_id":tip_state_id, "purpose":purpose, "original_path":str(source),
                        "original_name":source.name, "archive_path":str(destination.resolve()),
                        "parent_artifact_ids":list(parent_artifact_ids), "reused_content":previous is not None,
                        "producer_action_identity":"unresolved unless independently audited"}
                store.append_event('experiment_artifact_linked', link,
                    monotonic_ns=time.perf_counter_ns(), utc_ns=time.time_ns())
                links.append(link)
            store.finalize('complete', stopped_utc_ns=time.time_ns())
        except BaseException:
            store.finalize('failed', stopped_utc_ns=time.time_ns())
            raise
        return {"experiment_id":store.run_id,"database_path":str(store.database_path.resolve()),
                "manifest_path":str((store.run_dir/'manifest.json').resolve()),"registered":links,
                "authorizes_live_action":False}
