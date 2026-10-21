# Experiment recording and audit architecture

SPiRE's recorder associates telemetry, observed actions, original instrument files, derived analysis, and review results with a single run. The architecture aims to make available evidence traceable; it cannot reconstruct an acquisition tick, action, or file that was never observed or retained.

This document describes software structure. It does not enable any live instrument operation. Instrument actions remain subject to the typed Nanonis interface, capability registry, `SafetyGate`, fake backend, and operator authorization.

## Goals and limits

- Group related signals from one read request and retain request times, source channel names, units, and quality flags.
- Store SPiRE action requests, observed calls, readback, results, and errors with their actual provenance.
- Index source scans, spectra, analysis artifacts, and final views without treating a plot as the source measurement.
- Retain missed telemetry, reconnection gaps, incomplete writes, and unknown action provenance as explicit quality conditions.
- Mark a run complete only after its telemetry, events, and available artifacts pass the recorded completeness checks.

A software polling loop cannot establish that it captured every device acquisition tick when the device interface exposes only the latest value and no stable sequence counter. Ordinary process timing is not a hard real-time guarantee. Where a device-native logger is available and independently commissioned, compare its saved count and file rows with the SPiRE mirror.

## Run and signal records

Each run snapshots the resolved signal catalog by name, unit, and device index. Names are resolved at runtime; indexes are not assumed to remain stable across sessions. A telemetry sample contains a local sequence, monotonic time, wall-clock time, and data-quality flags. Channel values returned in one software read share a request interval; this does not establish that the hardware updated every channel on a single ADC cycle.

The configured gate determines when high-rate samples are retained. Closing the gate ends a signal segment; it does not by itself end the run. Run finalization records sample and event watermarks, commits the database, and captures report status. A reconnect, process rotation, or closed gate creates a real time interval without high-rate telemetry. Adjacent segments must not be presented as gap-free.

Writes use the existing `RunStore` and bounded queues. Queue or disk failures are surfaced in run status and logs; the recorder does not silently replace missing values. Per-run locking prevents a second SPiRE recorder from writing into the same data root, but does not coordinate unrelated external programs or grant instrument control.

### Arrival evidence and site-scoped gate values

The reviewed example profile uses the Z-controller state, a rolling current window, and Z travel clearance together. `Control On` is necessary but not sufficient evidence that the junction has reached the sample. The local arrival rule requires at least 8 valid samples spanning 1 s, median absolute current of at least 1.5 pA, a median `|I|/|setpoint|` ratio from 0.7 to 1.5, and Z clearance from both travel limits. A clearance failure vetoes the gate; being between the limits does not prove tunneling. The original installation's lowest setpoint range was about 2–3 pA. These numbers belong to [the reviewed example profile](../../src/knowledge_base/instrument_profiles/spire_local_station.toml) and must be rechecked before use at another station.

When the gate is closed, a short current spike is not arrival evidence: the historical withdrawn-signal trace contained roughly ±0.5 pA background variation and narrow excursions around ±2 pA. The window median, stable convergence to the setpoint, and Z-limit veto were used to avoid treating those excursions or a single status edge as a tunnel junction. A controller-on state with an accumulating, invalid, or out-of-range current window remains `candidate` and does not write high-rate signal rows. While `near_sample`, feedback-off or a new Auto Approach alone does not close the segment; the local policy closes after confirmed withdrawn state and a 1 s window of at least 8 samples whose median absolute current is at most 0.75 pA. This is a deterministic site policy, not an LLM judgement or physical interlock. Coarse motion is not continuously observable and cannot be inferred from the gate.

### Sampling deadlines and completeness

Each telemetry row is one wide sample from a single resolved-signal read, with an SPiRE sequence number, monotonic timestamps for ordering and elapsed time, UTC for display/cross-system correlation, a scheduled deadline, and quality flags. SPiRE's sequence is a client receipt sequence, not a device acquisition counter. Signal names are resolved again after reconnect; a changed device index starts a new stream. A common software read interval does not prove that the hardware updated every channel on one ADC tick.

The acquisition period is taken from the instrument configuration, but the recorder schedules latest-value reads against a monotonic clock. On the original reviewed controller, waiting for the newest value reduced observed cadence to about 25 Hz, so paced latest-value reads were used for an approximately 50 Hz client stream. This rate is a site observation, not a device-wide promise or proof of acquisition-complete coverage. If a read passes one or more scheduled deadlines, SPiRE records `missed_deadline` and `telemetry_gap`, estimates missed deadlines, and advances to the next future deadline. It does not busy-read to catch up or present a client average rate as the number of original Nanonis samples. A short pulse can also fall between polls; absence of a recorded peak means the peak was not observed, not that it did not occur.

The acquisition thread should only read, timestamp, validate, and enqueue. Keep rendering, compression, and parsing off that thread. Monitor the bounded writer queue and retain its high-water mark; do not drop the oldest sample to conceal pressure. When the queue reaches its danger threshold, record a health event before marking the run compromised. A gate-closed interval retains only state-transition events, not high-rate signal reads. The UI should subscribe to the recorder stream rather than create a parallel, unreviewed polling loop.

The active `RunStore` uses SQLite WAL with `synchronous=FULL` and one asynchronous writer to order samples, action events, and artifact metadata. A `Signals.ValsGet` read stores I/X/Y/Z as one wide row rather than four independently ordered rows. The default writer queue holds 5,000 items and reports a high-water mark at 80%; a full queue rejects the enqueue rather than silently dropping the oldest record. Action events and artifact/file records wait for durable commit before their caller proceeds, so a failed commit is surfaced before a protected workflow continues. Treat these as software durability properties, not proof that the device produced or returned every physical sample.

### Finalization order

Only a healthy recording accepts a mutating workflow action. Pausing a workflow stops new actions but does not stop I/X/Y/Z recording. If recording becomes compromised, reject new actions and let the workflow's explicit safety policy decide whether to stop; the recorder itself must not issue a withdraw. A normal close follows this order: stop accepting actions, wait for in-flight actions to reach a terminal event, stop the device logger, ingest available files, reconcile point counts and hashes, generate final artifacts, commit the watermarks, and only then mark the run `complete`. `COMPLETE` describes the configured record checks; it does not certify a successful experiment or recover data never returned to SPiRE.

## Actions and artifacts

Record actions at the source that observed them. A SPiRE action event can include intent, typed method, precheck, device response, readback, postcheck, outcome, and error. External UI activity remains unknown unless a supported, explicitly enabled observation source verifies it. Changes in telemetry, file timestamps, or process state do not prove who acted or what command was sent.

An artifact record retains a role, source name, content hash, media type, and relevant parent links. The original instrument file remains the scientific source; a derived plot or model output links to that source and preserves the analysis conditions. An artifact association does not demonstrate causation, sample identity, completeness, or scientific validity.

The session-directory observer records a baseline when it starts, so files already present are not presented as newly created by the current run. A supported file must retain the same size and modification time for at least two polls before its hash and artifact row are written. It records the source path and file facts without copying or moving the instrument file. Because directory observation alone does not identify a unique producing action, that association remains unresolved until a separate source proves it. Reanalysis creates a new artifact and does not overwrite an existing one; generated figures should name their raw parent and analysis version/conditions.

## Review and layer boundaries

The data layer provides transactional snapshots, paginated records, watermarks, and provenance. The analysis layer computes signal and image summaries without controlling a device. The workflow groups source records and derived artifacts into a review package. The runtime exposes only the explicitly configured CLI/MCP operations.

Review reports preserve source identifiers, quality warnings, parser limits, missing files, and hash mismatches. A report can state `complete` only when the configured checks pass. A complete software record is not the same as a successful experiment or physically verified result.

When a source file cannot be tied to one action event, retain it as unresolved rather than assigning it to the nearest action by timestamp. Keep the instrument-native scan or spectrum as the scientific source and link derived plots or models to their unique parent, together with analysis conditions. A screenshot, file association, shared timestamp, or changed telemetry value cannot alone establish who acted, prove sample identity, or attribute an outcome to an action.

## Development and commissioning

Exercise recorder logic with fake backends and synthetic or explicitly licensed fixtures. Check bounded queues, delayed reads, gaps, invalid channels, partial writes, process stop, and recovery without issuing live actions. Keep live commissioning separate and limited to the target installation. Before unattended collection, validate channel mapping, gate behavior, disk policy, permissions, stop and recovery behavior, and comparison with the device's native records where available.

The repository's station profile is a historical, scoped example. Its original acquisition records and logs are not included in this public release. The implementation, example thresholds, and synthetic tests do not certify another device or installation.
