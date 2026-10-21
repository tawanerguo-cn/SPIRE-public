# Changelog

## 0.0.101 — 2026-10-21

- Prepare the source tree for public release under the MIT License.
- Add installed `spire`, `spire-agent`, and `spire-recorder` commands, with private Nanonis site initialization, offline diagnostics, a persisted fake demonstration, and a Codex MCP configuration generator. Recorder supervision launches the installed module without depending on a source checkout.
- Load external connection and recording profiles without editing package source; resolve site paths against their defining configuration files. New draft profiles require review before live recording.
- Document the first-use and Codex adaptation workflows, and check base wheel installation separately from the full offline development suite.
- Remove private manuscripts, laboratory data transfers, site logs, and source references from the release tree.
- Keep reusable STM/Nanonis services, analysis, workflows, tools, tests, and scoped scientific methods. Remove private paths and sample/session identifiers from retained examples.
- Represent unpublished reference sources explicitly as unavailable, so historical methods remain readable and reproduction fails with a useful explanation before attempting file access.
- Clarify that laboratory references are contextual, that their source data are not included, and that live operation needs separate site validation and authorization.

## Earlier software milestones

Earlier pre-release versions established the seven-layer architecture, fake-first Nanonis access, capability and safety checks, traceable experiment records, shared CLI/MCP tool registration, offline scan and spectroscopy analysis, and bounded STM workflows. The manuscript submission and private laboratory iteration records are excluded from this public release.
