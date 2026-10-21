from __future__ import annotations

import ast
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
PROJECT_LAYERS = {
    "instrument_layer",
    "data_layer",
    "analysis_layer",
    "knowledge_base",
    "memory_store",
    "workflow_layer",
    "runtime_layer",
}
ALLOWED_LAYER_IMPORTS = {
    "instrument_layer": set(),
    "data_layer": {"instrument_layer"},
    "analysis_layer": {"instrument_layer", "data_layer"},
    "knowledge_base": set(),
    "memory_store": set(),
    "workflow_layer": {
        "instrument_layer",
        "data_layer",
        "analysis_layer",
        "knowledge_base",
        "memory_store",
    },
    "runtime_layer": PROJECT_LAYERS - {"runtime_layer"},
}


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


class LayerBoundaryTests(unittest.TestCase):
    def test_layer_imports_follow_the_architecture_direction(self) -> None:
        violations: list[str] = []
        for layer, allowed in ALLOWED_LAYER_IMPORTS.items():
            for path in (SOURCE_ROOT / layer).rglob("*.py"):
                project_imports = imported_roots(path) & PROJECT_LAYERS
                forbidden = project_imports - allowed - {layer}
                if forbidden:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)} imports {sorted(forbidden)}"
                    )
        self.assertEqual(violations, [])

    def test_only_official_backend_imports_socket_or_nanonis_spm(self) -> None:
        allowed = SOURCE_ROOT / "instrument_layer" / "backends.py"
        violations: list[str] = []
        for path in SOURCE_ROOT.rglob("*.py"):
            restricted = imported_roots(path) & {"socket", "nanonis_spm"}
            if restricted and path != allowed:
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)} imports {sorted(restricted)}"
                )
        self.assertEqual(violations, [])

    def test_recorder_cli_delegates_instrument_assembly_to_runtime(self) -> None:
        path = SOURCE_ROOT / "runtime_layer" / "recorder_cli.py"
        forbidden = imported_roots(path) & {
            "instrument_layer",
            "analysis_layer",
            "workflow_layer",
        }

        self.assertEqual(forbidden, set())


if __name__ == "__main__":
    unittest.main()
