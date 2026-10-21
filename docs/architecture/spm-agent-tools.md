# Saved-data tools for SPM agents

The CLI and optional MCP server expose reviewed tools through the same typed registry. They support file observation, analysis, explicit point selection, and fake-only workflow rehearsal. A fake rehearsal does not establish live instrument behavior.

## Install and start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[agent]"
.\.venv\Scripts\python.exe -B -m runtime_layer.agent_cli --help
```

The MCP server uses stdio and can be started by an MCP client. For file analysis, set the permitted input and output roots explicitly. For example:

```powershell
.\.venv\Scripts\python.exe -B -m runtime_layer.agent_cli --read-root $env:SPIRE_DATA_ROOT --output-root .\tmp\spm-agent --tool observe_scan --arguments-file .\tmp\observe-arguments.json
```

The input path in the arguments file must point to a local saved scan under the selected read root. This repository contains no sample SXM file to substitute. Results include source identity and analysis provenance; the tool does not modify the source file or use a live scan buffer.

## Available tools

| Tool | Behavior and limits |
| --- | --- |
| `observe_scan` | Returns raw/processed forward and reverse images, source identity, frame geometry, missing-pixel mask, and numerical candidates. Modes are `metrics`, `image`, and `joint`. |
| `view_scan_region` | Crops a previously observed scan using its observation ID; it preserves the source coordinate frame. |
| `prepare_atom_plan` | Validates explicit selections and excluded regions, then writes a `fake_only` plan. A visual candidate is not an identified species. |
| `inspect_spectrum` | Evaluates a specified channel and bias axis against an optional, explicitly scoped reference library. Insufficient or ambiguous evidence returns `unknown`. |
| `run_fake_atom_plan` | Validates provenance and replays the existing workflow against a fake backend. It does not perform live spectroscopy. |
| `inspect_atom_run` | Reads saved run events, including partial and failed runs; it does not rerun them. |
| `inspect_recording` | Summarizes a saved RunStore and eligible local artifacts. It does not connect to an instrument. |

Tool names and access guidance come from `knowledge_base/script_manifests/spm_agent_tools.toml`; parameter validation and dispatch use the shared `ToolRegistry`. The model cannot use an arbitrary shell, Python interpreter, desktop, or direct hardware socket through this tool list.

## Selection and interpretation

The operator or model must make scan selections explicit and provide a rationale. The tools return the pixel coordinates, frame conversion, source identity, and error bounds required to review a selection. A plan is rejected when its recorded source identity or geometry no longer matches the input. A saved plan remains fake-only, and its excluded regions constrain landing points rather than the path between them.

Selections require a valid, acquired 3×3 pixel neighbourhood and cannot overlap an excluded landing region or duplicate another selected point. Keep the source observation ID, source content, frame geometry, and image coordinate transform together: the plan is rejected if its source or geometry changes. A hash match only says the file is byte-identical; it does not show that the present tip is still in the historical image coordinate frame. Historical coordinates and saved plans are not live landing instructions.

### Candidate detector limits

`observe_scan` defaults to a 3-MAD threshold, 5-pixel minimum separation, 1-pixel smoothing radius, and 12-pixel local-background radius. These are adjustable observation settings, not laboratory-reviewed atom-detection thresholds. In one reviewed scan set, a global MAD baseline missed features against terraces or broad background variation, while too-small minimum separation counted one broad feature more than once. A candidate score is not a calibrated probability, and the number of candidates is not an atom count.

Retain partial-acquisition masks; never stretch a partial scan to make it look complete. Inspect raw forward and reverse channels, physical scale, local background, and the source region around every candidate. Use an explicit local ROI when terraces or broad background change the baseline. Treat bright/dark features, steps, scan artefacts, overlap, and merged objects separately; registration and candidate overlays are aids to review, not physical identification. These failure modes describe one baseline detector and one reviewed scan set, not a universal performance guarantee.

### Reference-spectrum limits

A reviewed reference must match its declared material/system and measurement conditions. The software checks the reference's explicit conditions but cannot prove they are complete or that the recorded values are true. A spectrum is comparable only when its channel and units match and its bias axis is monotonic and compatible with the reference; the current method compares unit-normalized signals with L2 distance. It does not interpolate or extrapolate between axes. Missing or non-finite values, constant signals, an invalid axis, no applicable reference, excessive distance, or ambiguous labels produce `unknown`.

`maximum_distance` and `minimum_margin` are recorded analysis settings, not validated laboratory acceptance limits. A `confidence` value is derived from similarity and is not a probability of chemical identity. A channel such as `LI Demod X (A)` is not automatically calibrated dI/dV in A/V. Preserve an unknown result rather than force a species label when scan and spectrum disagree or when required conditions are missing.

Numerical candidates and spectrum labels are review aids. Image registration, contrast, signal quality, and physical interpretation must be checked against the original data and relevant experiment conditions. No general accuracy claim has been established for these heuristics or for a model interpreting their output.
