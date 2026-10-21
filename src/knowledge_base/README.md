# Knowledge base

Use the entry for the current task and keep each method in its existing home.

| Task | Entry |
| --- | --- |
| STM tip conditioning and stop criteria | [Workflow](tip_conditioning/workflow.md) and [quality rubric](tip_conditioning/quality_rating.md) |
| Material-scoped scan, signal, or spectroscopy observations | [Tip-conditioning references](tip_conditioning/README.md) |
| Instrument behavior and profile settings | [Instrument profiles](instrument_profiles/README.md) |
| Reviewed image or signal references | [Tip-quality references](tip_quality_references/README.md) |
| Literature methods and scope | [Literature index](literature/README.md) |
| Tool contracts, recording patterns, and GUI signal names | `script_manifests/`, `recording_patterns/`, and `gui_action_bindings/` |

The station and material records describe historical site observations. Their raw images, spectra, RunStores, and audit bundles are not part of this public release. A summary is not a substitute for the source data and does not provide a reproducible benchmark. Check each profile against the target instrument and approved operating limits before use.

Keep this knowledge separate from execution permissions. Instrument actions are governed by the typed services and `SafetyGate`; a workflow or reference entry cannot expand those limits. See the [contributor guidance](../../AGENTS.md#knowledge-base) for maintenance rules.
