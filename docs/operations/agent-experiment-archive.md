# Experiment archive

SPiRE can register artifacts in a `RunStore` with an experiment ID, sample identity, purpose, provenance, and parent links for derived analyses. This workflow preserves the original file and records its content hash; it does not infer sample identity from a filename or infer causation from temporal order.

## Start an archive

Use a new, empty data root and explicit identifiers from the local experiment record. The values below are placeholders:

```powershell
python -B -m runtime_layer.recorder_cli archive `
  --root "$env:SPIRE_DATA_ROOT" `
  --experiment-id example-experiment `
  --sample-id sample-unknown `
  --sample-description "Sample identity awaiting operator confirmation" `
  --identity-source "unknown" `
  --tip-state-id tip-state-unknown `
  --purpose "offline example"
```

Keep the returned database path for later additions to the same experiment. Do not copy identifiers from this example into real work. The identity fields record what is known at archive creation; an operator's later correction should be added as a sourced annotation instead of rewriting the original record.

## Add source and derived files

```powershell
python -B -m runtime_layer.recorder_cli archive `
  --database "$env:SPIRE_RUNSTORE" `
  --tip-state-id tip-state-unknown `
  --purpose "offline example" `
  --artifact "raw_scan=$env:EXAMPLE_SCAN" `
  --artifact "analysis=$env:EXAMPLE_ANALYSIS" `
  --parent-artifact-id ARTIFACT_ID
```

`--artifact ROLE=PATH` may be repeated. Use roles such as `raw_scan`, `raw_spectrum`, `analysis`, or `operator_label`. Each entry retains the source filename and content hash, its role, tip state, and purpose. A derived file should refer to its parent artifact. Do not edit or rename the source file as part of archival.

Within one experiment, byte-identical files may share a content object, but every registration still records the original filename and role so a duplicate save does not erase provenance. Preserve the acquisition time recorded inside a source file separately from the time SPiRE observed or saved it; a resaved cached scan is not a new acquisition. File prefixes, directory names, and timestamps are clues to check, not sample identity or proof that two artifacts belong together.

Use a parent-artifact link only when the source is uniquely identified. If a derived plot, fit, or label could have come from more than one raw parent, keep its association unresolved instead of choosing the nearest timestamp or a plausible filename. Record the analysis method and conditions alongside the derived artifact. A complete hash proves byte identity after registration, not that an artifact is fresh, scientifically valid, or causally linked to an action.

Unknown action provenance and uncertain sample identity should remain explicitly unknown. Operator labels, software analysis, and scientific interpretation are separate record types. File association alone does not establish that an action caused an outcome.

The archive can index files and retain provenance. It does not move files out of the source location, establish the physical identity of a sample, or certify that a measurement passed scientific review.

## Local and public data

RunStores, raw instrument files, derived plots, and deployment settings can contain private laboratory data. Keep them in the configured local data root. This release excludes private raw archives, session logs, RunStore databases, and transfer bundles. Tests include de-identified recorded regression traces and synthetic inputs, neither of which is a complete archive or substitutes for source measurements. Knowledge-base summaries retain bounded historical observations, but their original source files are not present here and cannot be fetched or reproduced from this repository.
