# Recording review summaries

A review summary is a derived view of a saved `RunStore`; the run's raw events, telemetry, and artifact index remain authoritative. Review is read-only and does not open an instrument connection.

## Output and command

After the writer stops and the database is finalized, the recorder can generate a `recording_review/` directory containing:

- `summary.json` with status, event timeline, telemetry checks, artifact checks, limitations, and the database read watermark;
- `summary.md` with the same evidence in human-readable form;
- `annotations.json` with sourced importance labels associated with action event IDs;
- `recording_review_status.json` with review success or the error that prevented a report.

Run the review command on an existing database, using local paths supplied by the operator:

```powershell
python -B -m runtime_layer.recorder_cli review "$env:SPIRE_RUNSTORE" --output "$env:SPIRE_REVIEW_OUTPUT" --read-root "$env:SPIRE_DATA_ROOT"
```

Choose a new output directory. Every database and source file must fall within an explicitly authorized read root. The review does not write to the source database. A live snapshot is marked unfinalized and is not evidence that a recorder process is still running.

## Interpretation

| Label | Meaning |
| --- | --- |
| `background` | Repeated status observation whose information is limited by a confirmed enclosing process. |
| `normal` | Independent state change, event boundary, or ordinary artifact evidence. |
| `attention` | Action failure, safety condition, incomplete run, or missing/invalid data that affects interpretation. |

An annotation is a sourced classification of the saved record, not a replacement for it. The review preserves raw event IDs, quality flags, and unavailable evidence. It does not invent actions from a change in a signal. It does not infer sample identity from filenames or pair old files by timestamp alone.

Only repeated state observations that are `observed`, have `actor=unknown`, carry a complete payload, and occur at least twice within a positively observed Auto Approach interval can be summarized as low-increment `background` information. Failure, protective intent, SafeTip, explicit operator/SPiRE actions, and events without confirmed process context remain visible as `attention` or `normal`. The group retains every original event ID and its start/end times. Its count is a count of state observations, not a count of manual withdrawals, commands, or approach actions. An approach without an observed end remains an open episode and stays prominent.

Treat `telemetry_gap` counts as timing-quality evidence, not as a number of missing samples: one timing excursion can produce a warning, while the estimated missed deadlines and maximum interval convey the measured gap. Compute cadence and maximum sample spacing within each high-resolution segment. Do not count the idle interval between withdrawal and re-engagement as a sampling pause inside one continuous segment, and do not join segments into a continuous trace. Scan and Bias Spectroscopy running intervals are evidence separate from their saved files. A filename or nearby timestamp does not prove sample identity or acquisition pairing. A Follow Me position readback within one second of scan stop is ambiguous between scanner settling and an independent manual movement.

## Analysis and access limits

The data layer reads a consistent database snapshot and applies bounded pagination. The analysis layer computes timelines, telemetry quality, and file checks. The workflow reuses the existing saved-file readers, verifies source hashes when available, and writes a separate report. The CLI/MCP runtime supplies the explicit path roots; it does not start a live device connection.

Reports can be incomplete when the run was truncated, source files are missing, hashes differ, or parser limits are reached. These conditions remain visible and do not count as acceptance. Review accuracy depends on the source records; it cannot restore samples that were never stored or prove hardware behavior from a screenshot or summary alone.
