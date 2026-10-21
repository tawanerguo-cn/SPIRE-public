# SPiRE

**Scanning Probe intelligence for Research Experimentation** is an extensible software platform for scanning probe microscopy (SPM). Its current implementation focuses on STM instruments controlled through Nanonis. It connects typed instrument services, safety checks, data access, analysis, experiment workflows, and CLI/MCP runtimes.

The release excludes private laboratory archives and data-transfer packages, private instrument configuration, and the research manuscript. Tests include de-identified recorded regression traces as well as synthetic inputs; neither is a complete experiment archive or a substitute for the original measurements. Site-specific examples in the knowledge base are historical references, and their settings must be checked for each instrument and experiment.

New installations: start with [Getting started](docs/getting-started.md) and [Codex adaptation](docs/codex-adaptation.md). The setup flow targets Nanonis; it does not claim support for other instrument vendors.

## Start offline

Python 3.11 or newer is required. For a first-use local setup, create an isolated lab profile and run the offline demo:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\spire.exe init --directory .local/lab
.\.venv\Scripts\spire.exe doctor --site .local/lab
.\.venv\Scripts\spire.exe demo --output .local/demo
```

The demo uses fake data and does not connect to an instrument. `spire doctor --site .local/lab` checks the site configuration without claiming that hardware has been commissioned. Install `.[agent]` when adding Codex MCP tools. The optional Nanonis capability report also needs the vendor SDK:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[live]"
.\.venv\Scripts\python.exe -B tools/nanonis_capability_report.py
```

For development, install all offline-test extras including the Nanonis SDK, then run the suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[agent,api,validation,live]"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

For the CLI/MCP tool list and parameters, run `spire-agent --help` or `python -m runtime_layer.agent_cli --help`. Read-only file tools require explicit input and output roots. Live instrument access is a separate, opt-in deployment that requires a configured Nanonis connection and the operator's authorization. Offline tests do not verify physical instrument behavior.

## Architecture

The seven layers live under `src/`:

| Layer | Responsibility |
| --- | --- |
| `instrument_layer` | Typed instrument access, capability declarations, and safety checks |
| `data_layer` | File access, coordinate handling, provenance, and `RunStore` |
| `analysis_layer` | Reusable image, signal, and spectroscopy analysis |
| `knowledge_base` | Reviewed methods, instrument profiles, references, and tool manifests |
| `memory_store` | Reserved layer; automatic retrieval and prompt assembly are not implemented |
| `workflow_layer` | Experiment orchestration and evidence handling |
| `runtime_layer` | CLI, MCP, and service assembly |

See [platform and science boundaries](docs/architecture/platform-and-science.md), [instrument services](docs/architecture/instrument-services.md), and the [knowledge base](src/knowledge_base/README.md).

## Scope

STM/Nanonis is the primary supported instrument path. Fake-backed tests cover software behavior; users must validate their own instrument configuration, permissions, sample conditions, and operating limits. AFM support and other device backends are not implemented. Laboratory reference values and observations describe their original context; they are not universal thresholds or a substitute for instrument commissioning.

## License

SPiRE is released under the [MIT License](LICENSE).
