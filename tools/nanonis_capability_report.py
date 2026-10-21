from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrument_layer import (  # noqa: E402
    CapabilityImplementation,
    build_nanonis_capability_registry,
    summarize_capabilities,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report the local Nanonis capability registry status.")
    parser.add_argument("--include-helpers", action="store_true", help="Include public nanonis_spm helper methods.")
    parser.add_argument("--show", choices=["implemented", "bounded", "all"], default="implemented")
    args = parser.parse_args(argv)

    try:
        capabilities = build_nanonis_capability_registry(include_helpers=args.include_helpers)
    except ImportError as exc:
        print(f"failed to import nanonis_spm: {exc}", file=sys.stderr)
        print("Install the live optional dependency with: python -m pip install 'nanonis-spm>=1.0.9'", file=sys.stderr)
        return 2

    summary = summarize_capabilities(capabilities)
    print(f"total={summary.total}")
    print(f"by_implementation={summary.by_implementation}")
    print(f"by_access={summary.by_access}")
    print(f"by_safety={summary.by_safety}")

    selected = _select_rows(capabilities, args.show)
    if selected:
        print()
        print("nanonis_method\tstatus\taccess\tsafety\twrappers\tbounded_action")
        for item in selected:
            print(
                "\t".join(
                    [
                        item.nanonis_method,
                        item.implementation.value,
                        item.access.value,
                        item.safety.value,
                        ",".join(item.wrapper_methods),
                        item.bounded_action or "",
                    ]
                )
            )
    return 0


def _select_rows(capabilities, mode: str):
    if mode == "all":
        return capabilities
    if mode == "implemented":
        return [
            capability
            for capability in capabilities
            if capability.implementation == CapabilityImplementation.IMPLEMENTED
        ]
    if mode == "bounded":
        return [capability for capability in capabilities if capability.bounded_action]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
