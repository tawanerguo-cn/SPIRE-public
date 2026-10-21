# Instrument access and authorization

SPiRE separates offline analysis from live instrument control. The default backend is fake. Offline inspection tools read only the files and output directory supplied by the operator; they do not connect to Nanonis or move an instrument.

## Offline use

Use the capability report to inspect the operations implemented by the current checkout:

```powershell
python -B tools/nanonis_capability_report.py --show all
python -B -m runtime_layer.agent_cli --help
```

File-analysis commands require explicit read and output roots. Keep source data under a locally selected root and send derived files to a separate output directory. Review the source filename, acquisition conditions, and provenance before interpreting an analysis result.

## Live access

Live actions are available only through the typed Nanonis device interface. The capability registry describes supported operations; the `SafetyGate` enforces registered bounds; the audit path records action requests and outcomes. CLI, MCP, and workflow code must use those shared services rather than open a socket or issue raw instrument commands directly.

A deployment operator must configure the instrument endpoint privately and explicitly enable the required live path. The checkout's fake default, local authorization, instrument configuration, and physical safety interlocks are separate conditions. Passing one does not imply the others. A capability report or fake test describes software behavior; it does not verify the connected instrument or physical effect.

Site profiles and numerical limits are installation-specific. Review them against the installed instrument, approved operating procedure, current sample, and physical interlocks before enabling live access. The sample values in this repository are scoped historical references; they are not universal presets and do not expand the `SafetyGate` limits.

## Operator handoff and uncertain actions

When an operator takes control or asks the agent to stop, stop automated instrument actions and yield control. A previous authorization does not prove that the agent still owns the instrument. If an action's completion is uncertain, inspect its audit record and read back the instrument state before deciding what to do; do not resend it automatically.

Separate measurement quality from instrument protection. A parse error, missing sample, or disappointing result does not by itself establish a physical fault or authorize another action. Preserve original errors and available readback for review. Ask the operator for physical intervention when software cannot make safe progress.

## Knowledge and analysis boundaries

Use the [tip-conditioning workflow](../../src/knowledge_base/tip_conditioning/workflow.md) only within its stated STM scope, and read the [quality rubric](../../src/knowledge_base/tip_conditioning/quality_rating.md) before interpreting ratings. These documents describe decision support. They do not authorize execution, and numerical examples must be checked against the local profile.

Saved-scan inspection and quality summaries are offline analyses. A candidate classification is an observation for review, not an identified species, a verified instrument state, or permission to move. See [SPM agent tools](../architecture/spm-agent-tools.md) for the read-only tool boundaries and [instrument services](../architecture/instrument-services.md) for the typed access boundary.

## Interpreting finite pulse checks

`pulse_and_assess` samples one finite observer across a baseline, the typed command interval, and a post-action window. The default baseline is 0.2 s; the post-window duration and settling prefix are explicit request parameters. A baseline must contain at least three sampling intervals. This is software polling around a command, not a hardware-triggered waveform. The pulse itself is not retried by the quick-check path.

| Field | Meaning and limit |
| --- | --- |
| `peak_abs_current_a` | Largest absolute current observed during the sampled command interval. It may be `null` if that interval had no usable rows. |
| `near_full_scale` | Whether the sampled peak reached 99% of the explicitly supplied current range. `false` means the sample stream did not record a near-rail value; it does not prove that no faster transient reached the rail. |
| `current_before_a`, `current_after_a` | Median current in the corresponding valid windows; the assessment also reports the after-window error relative to setpoint. |
| `current_rms_before_a`, `current_rms_after_a`, `current_rms_ratio` | Time-domain window variation and post/pre ratio. They do not represent bandwidth-integrated spectral power. |
| `delta_z_m`, `z_direction` | Difference between the post- and pre-window instrument Z readbacks. It is not an independent physical gap measurement. |
| `comparison_valid`, `window_flags` | Whether usable, same-XY pre/post windows and sampling-gap constraints support comparison. Missing or invalid samples remain flagged; they are not filled with zero. |

The command interval is sampled rather than hardware-triggered and `peak_complete` remains false. A 10 ms pulse may be shorter than a roughly 20 ms polling interval, so the captured maximum can miss its peak. Use an observed peak as a response clue only. Likewise, a small `delta_z_m` or favourable peak does not outweigh an unstable recovered current.

A `stable_candidate` means only that the current window satisfies the configured absolute screen. It does not show improvement relative to a preceding pulse, persistence across later windows, adequate morphology, or usable spectroscopy. Compare matched windows at the same position and working point; then review improvement and whether it holds, and inspect the image or spectrum required by the next experiment. If no quality threshold was supplied, report measured facts without assigning a pass. These site observations and parameters are recorded in [the tip-conditioning workflow](../../src/knowledge_base/tip_conditioning/workflow.md); they are not universal acceptance limits.
