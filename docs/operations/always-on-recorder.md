# Continuous experiment recorder

The recorder stores signal samples, observed actions, source files, and review metadata in a `RunStore`. Continuous recording is optional and disabled by default. It observes the instrument state; it does not approach, withdraw, change settings, or restart Nanonis.

## Sampling and data quality

Each run resolves signal names against the current instrument catalog and records the names, units, and catalog snapshot in its manifest. A single signal-read request preserves a common software request interval; it does not prove that every hardware channel updated on the same ADC tick.

Profiles may include the I/X/Y/Z channels and, where the instrument exposes them, lock-in X/Y channels in the same read. The recorder stores raw lock-in values and their provenance. It does not enable modulation, rotate phase, or convert them into conductance. Missing, invalid, or late measurements remain explicitly marked; the recorder does not fill them with zero or fabricate a continuous trace.

The near-sample gate controls whether high-rate telemetry is written. The recorder's low-rate action timeline has its own configured start condition. A database record or a healthy recorder process does not prove that every instrument acquisition tick was captured. Use the source instrument logger, where configured and validated, to independently reconcile acquisition completeness.

## Runs, service, and storage

`record` writes one run and finalizes its review on normal exit. `supervise` rotates bounded runs, publishes small health snapshots, and retains gaps, failed runs, and incomplete finalization status. A segment boundary, reconnect, closed gate, or scheduled rotation can create a real acquisition gap. Review timestamps and quality flags instead of stitching adjacent runs into a continuous interval.

Keep one recorder instance per data root. Do not hot-edit the configuration used by a running process. When another authorized program needs the instrument connection, stop the recorder and confirm the current run has finalized and its worker has exited. Starting a replacement before the prior process exits can duplicate collection.

The storage quota and warning threshold are configured in the local recording settings. Estimate actual growth with the recorder's status/estimate commands, monitor free space, and preserve raw files. A planning estimate is not a guarantee of capacity.

For rough planning, one historical estimate for a 50 Hz I/X/Y/Z stream used 256 bytes per sample per source, or about 12.5 KiB/s and 1.03 GiB per full recording day for each source. Actual SQLite pages, indexes, WAL, events, native logger files, and duty cycle change the result; use live size estimates and free-space checks for the target installation. The supervisor waits when the configured quota is reached or physical free space is below its configured minimum, and it retains raw files rather than pruning them.

## Health, retries, and stop handoff

Health snapshots are written to a temporary file and atomically replaced so a reader sees either the old complete snapshot or the new one. On Windows, a brief sharing denial permits at most three bounded retries totaling 35 ms. This retry is limited to the small health file; it never reissues an instrument command, and any delay remains visible to telemetry cadence checks.

A worker reports `telemetry_cadence` separately from other component failures. A missed acquisition deadline marks the evidence compromised, but the supervisor keeps the worker collecting rather than turning that recorded gap into a stop-and-retry gap. A normally finished segment with only this timing warning can rotate immediately while retaining its compromised status, exit code, and gap evidence. Writer failures, other component failures, a stale worker heartbeat, or a watchdog timeout follow the configured stop and retry path. Repeated worker failures use bounded 5, 10, 20, 40, then 60 s backoff; each retry starts a fresh run, and a failed or unfinalized prior run is preserved. This process policy does not replay an action or reconnect by resending a device command.

Before starting a replacement worker, the supervisor waits for the prior process to exit and acquire the data-root lease. On requested stop, it signals the worker, waits for finalization, and only then returns control; a forced termination means the run may be unfinalized, so retain its database and raw files for inspection. The data-root lock prevents two SPiRE recorders sharing that root, not unrelated clients or recorders using another root.

Separate Nanonis Programming Interface connections separate request/response traffic, but bias, feedback, scan and spectroscopy state and instrument/host resources remain shared. A second port does not prove concurrent measurements are harmless. Use a stop-and-confirm handoff when development work needs exclusive access, and resume only through the explicit enable/start path after the operator has confirmed the instrument state. A stale heartbeat, enabled flag, GUI closure, or timeout does not establish that the previous worker exited or that the instrument is safe to resume.

## Install and operate

After reviewing the local profile, prepare a fixed-version deployment directory outside the development checkout. Copy only the operator's private recording configuration into it. The example below uses placeholders:

```powershell
python -B -m runtime_layer.recorder_cli enable
./tools/install_recorder_task.ps1 -RuntimeDirectory $env:SPIRE_RUNTIME -PythonExecutable $env:SPIRE_PYTHON
```

Check the service and stop it explicitly when needed:

```powershell
python -B -m runtime_layer.recorder_cli status
python -B -m runtime_layer.recorder_cli stop-service
```

Review is generated after a normal foreground `record` run. Long-running service runs can be reviewed separately after they have finalized. An incomplete or forced-stop run needs inspection; the presence of a database file does not establish completeness.

## Site configuration

The repository's instrument profile and recording settings are examples scoped to the original installation. Select current signal names from the connected instrument, set a local data root and storage policy, and validate recording behavior at the target acquisition rate before unattended use. Keep private settings, data, logs, and session identifiers outside this public source tree.
