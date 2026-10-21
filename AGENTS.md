# Contributor guidance

SPiRE is an SPM automation platform. STM with Nanonis is the current instrument implementation. Scientific workflows consume shared platform services; they must not introduce parallel instrument access or assume that site-specific knowledge applies to every installation.

## Architecture

Seven layers live directly under `src/`: `instrument_layer`, `data_layer`, `analysis_layer`, `knowledge_base`, `memory_store`, `workflow_layer`, and `runtime_layer`.

- Keep the official Nanonis backend behind the typed device interface. Higher layers must not access raw sockets, device protocols, or vendor APIs directly.
- Keep hardware access fake-first. Ordinary tests must use the fake backend and must not connect to an instrument.
- Route live actions through the existing capability registry, typed interface, SafetyGate, and audit path. Documentation, prompts, and reference values do not grant permission.
- Stop agent control when the operator takes over; do not send automatic cleanup or withdrawal commands after control has been taken away. A previous authorization does not restore control.
- Send each hardware action once. If completion is unknown, preserve the original request and error, then use separately authorized readback; do not blindly retry the action.
- Do not use desktop automation, raw TCP, or vendor calls to bypass missing typed capabilities. Direct coarse motor motion toward the sample remains prohibited.
- Preserve source, units, sample identity, and uncertainty when storing or analyzing measurements. Unknown provenance remains unknown.
- Keep site profiles and scientific observations scoped to the instrument, sample, and conditions from which they came. Do not silently promote them to generic defaults.

## Changes and verification

Keep first-use setup and lab-specific Codex configuration in [Getting started](docs/getting-started.md) and [Codex adaptation](docs/codex-adaptation.md), respectively. The setup path currently targets Nanonis only.

For a new laboratory, read those guides before editing code. Use `spire init`, `spire doctor`, and `spire demo` to establish an offline baseline. Store site files in the ignored `.local/` directory. Record missing Nanonis versions, signal names, units, calibration, and operating limits explicitly; do not infer them from the bundled historical reference profile. Initialization and offline tests do not authorize a connection to real hardware.

Before changing a behavior, inspect its existing implementation and callers. Extend the single responsible layer, and update its existing documentation and tests. Avoid compatibility shims, duplicate execution paths, or unrelated refactoring.

Use fake data for hardware-related regression checks. Tests that require a real instrument must be explicit opt-in and must never run as part of the normal offline suite. Do not trigger additional hardware actions for testing.

For Python changes, run the relevant offline tests and, when scope warrants, the full suite:

```powershell
python -B -m unittest discover -s tests -v
```

Before finishing, review `git diff --check`, changed files, and any references to private configuration or laboratory data. Update `pyproject.toml`, `CHANGELOG.md`, and this README when a released behavior or public interface changes. A local commit does not authorize pushing or publishing.

## Knowledge base

Keep decision-making methods in the existing knowledge-base entry for that subject. Describe what to observe, how to judge it, what to do next, and which conditions bound the conclusion. Keep operator judgments, measured results, and hypotheses distinct. Retain only data and thresholds needed to make or reproduce a decision, and make their sample, instrument, and operating conditions explicit.

Do not copy private laboratory data, credentials, personal identifiers, local paths, or private session logs into public source, fixtures, or documentation. When a public example cannot retain its source data, say so plainly and remove links that suggest the data can be fetched or reproduced.
