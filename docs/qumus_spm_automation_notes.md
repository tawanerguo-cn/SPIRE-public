# Research note: embodied laboratory-agent design

This note summarizes design ideas from Shi et al., “Qumus: Realization of An Embodied AI Quantum Material Experimentalist” ([arXiv:2605.18407](https://arxiv.org/abs/2605.18407)). It is a literature-derived proposal, not a description of capabilities implemented or validated by SPiRE.

## What The Paper Actually Built

Qumus is an embodied, multi-agent lab system for 2D materials. It automates mechanical
exfoliation, optical flake search, transfer, and vdW stacking. The most useful part for
us is not the robotics hardware itself, but the software boundary:

- A lead agent interprets the scientific request, creates a plan book, delegates to
  specialist agents, checks feedback, revises the plan, and writes a final report.
- Specialist agents have narrow responsibilities: project knowledge, lab readiness,
  device/layout design, and physical processing.
- Low-level hardware commands are deterministic tools. The LLM chooses among valid
  tools and recipes, but does not freely synthesize unsafe hardware commands.
- Skills are organized into three levels:
  - Atom workflows: fixed unit operations, such as stage movement, camera focus, or
    heater control.
  - Molecule workflows: composed procedures, such as exfoliation, auto-search, or
    flake relocation.
  - Assembly workflows: long multi-stage procedures, such as repeated exfoliation or
    device stacking.
- Reusable recipes store optimized parameters and can be retrieved in later runs.
- Persistent state is split into Materials, Project, and Experiment databases so every
  final result can be traced back to materials, images, workflows, agent decisions,
  and tool outputs.

## The Key Transfer To SPM

For an SPM system, the paper suggests exploring an architecture that:

- Keep Nanonis commands as bounded atom tools.
- Let the agent compose only approved molecule and assembly workflows.
- Treat expert protocols and recipes as first-class, versioned lab knowledge.
- Log every observation, decision, command, parameter, data file, and outcome.
- Make the AI responsible for interpretation, planning, parameter selection, and
  recovery, not raw hardware improvisation.

## Agent Mapping

Qumus role -> SPM role:

- Lead Qumus agent -> STM Orchestrator
  - Parses requests like "repair the tip", "find isolated atoms", or "map ESR spectra".
  - Builds the plan book and owns final decision making.

- Project Manager Agent -> Protocol and Physics Expert
  - Retrieves prior recipes, local histories, papers, and sample-specific constraints.
  - Suggests initial parameters and allowed exploration ranges.

- Lab Manager Agent -> Instrument and Sample State Manager
  - Checks Nanonis connection, scan state, Z-controller state, bias/current/setpoint,
    session path, recent files, and optional cryostat/lock-in/magnet status.
  - Decides whether the system is ready for a workflow.

- Device Expert Agent -> Surface/Atom/Spectroscopy Expert
  - For ESR-STM, chooses atoms/sites, spectroscopy positions, bias ranges, RF settings,
    and labeling criteria.
  - For tip repair, evaluates whether image/signal quality indicates a usable tip.

- Processing Agent -> Nanonis Workflow Executor
  - Runs approved Nanonis-backed workflows and returns structured reports.

- Analyzer Agent -> Image/Spectra/Signal Analyst
  - Classifies scan frames, Signal Chart traces, STS/ESR spectra, and tip-quality
    metrics. This can be rule-based first and VLM/ML-assisted later.

## Proposed SPM Workflow Levels

Atom tools, currently or near-term:

- `get_status_snapshot`
- `read_signals`
- `grab_scan_frame`
- `list_recent_data_files`
- `stop_scan`
- `stop_follow_me`
- `stop_motor`
- `withdraw_tip`
- later: bounded bias pulse, tip shaper, Z ramp/poke, scan start/stop, spectroscopy
  acquisition, lock-in/RF parameter set.

Molecule workflows:

- Instrument readiness check.
- Tip-quality observation from current/Z traces and recent scan image.
- Controlled pulse recipe with pre/post checks.
- Tip-shaper recipe with strict parameter bounds.
- Re-image same area after conditioning.
- Atom candidate detection from scan image.
- Spectrum acquisition with automatic quality checks.

Assembly workflows:

- Automatic tip repair loop:
  1. Observe tip state.
  2. Classify failure mode.
  3. Pick a bounded conditioning recipe.
  4. Run safety gate.
  5. Execute fixed workflow.
  6. Re-observe and score.
  7. Stop on success, max attempts, or unsafe state.

- ESR-STM atom campaign:
  1. Scan target area.
  2. Detect and rank atom candidates.
  3. Move to each atom.
  4. Acquire spectroscopy/ESR traces.
  5. Fit and label atom properties.
  6. Persist atom map, spectra, fit parameters, and confidence.

## Databases We Should Add

The Qumus database split maps naturally to:

- Sample/Instrument database:
  - sample id, area id, tip id, scan frame, atom coordinates, data files, instrument
    state snapshots, recipe-compatible status.

- Workflow database:
  - workflow templates, parameters, parameter bounds, safety gates, workflow runs,
    step logs, artifacts, preconditions, postconditions.

- Experiment database:
  - user sessions, orchestrator runs, agent calls, reasoning summaries, tool calls,
    tool responses, decisions, final reports.

This traceability is especially important for tip repair, where a useful result may come
from a sequence of failed attempts.

## Safety Design Principle

The paper's most important safety pattern is: fixed low-level operations, flexible higher-level
planning. For SPM this should be stricter than Qumus:

- LLM cannot issue raw Nanonis commands.
- Every hardware-changing action must be a named workflow with typed parameters.
- Each workflow declares allowed ranges, preconditions, abort conditions, and recovery.
- Read-only observation tools are broadly available; motion, pulse, and approach tools
  require explicit safety gates.
- Failed or ambiguous analyzer output should route to observation/retry or human review,
  not direct actuation.

## Qumus Failure Lesson

In the hBN demo, the system recovered from two failures: a human removed the chip, and an
LLM/agent mislabeled the material. The useful lesson is that recovery should be driven by
database and observation consistency checks. For us:

- Do not trust the agent's label of "good tip", "atom found", or "spectrum succeeded"
  unless it is backed by measurements and stored artifacts.
- Every step should have a measurable postcondition.
- If the postcondition fails, the next action should be a predefined recovery branch.

## First Implementation Target

These are research directions. The current implementation and supported entry points are documented in the repository README and architecture pages:

- Define `AtomTool`, `MoleculeWorkflow`, `AssemblyWorkflow`, `Recipe`, and `SafetyGate`.
- Register verified read-only Nanonis tools and protected stop tools.
- Evaluate fake-backend workflows for observation, decision logging, and blocked actions.
- Extend run records to retain the evidence needed to review any future live action.

None of these proposals should be read as a report of completed capability. Live execution, acceptance criteria, instrument permissions, and physical safeguards remain installation-specific.
