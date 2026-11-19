#!/usr/bin/env python3
"""Run the ontology conformance gate against this repo's own adapter.

Standalone-checkout entry point. The workspace-root
``scripts/ontology_conformance_gate.py`` cannot be used from CI: each repo is
checked out on its own, and the kernel has **no git remote** to fetch (it is not
a git repository). So each surface keeps a one-entry registry next to itself
(``ontology-adapter.json``) and this launcher pins it.

Usage::

    python3 scripts/ontology_gate.py            # check this repo's adapter
    python3 scripts/ontology_gate.py --list     # what is registered here
    python3 scripts/ontology_gate.py --adapter fd-wire

Exit codes are the gate's: ``0`` passed, ``1`` violations (each printed with its
fix), ``2`` the gate could not run.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "ontology-adapter.json"

# The registry's `paths: ["."]` is resolved against the registry file, so this is
# belt-and-braces: it also makes the package importable when the registry is
# passed explicitly from elsewhere.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from fd_ontology_kernel.conformance.gate import main
except ModuleNotFoundError:  # pragma: no cover - CI installs the wheel first
    raise SystemExit(
        "闸门 · 前置 · 装不到 fd_ontology_kernel · "
        "修法：先装本仓 vendored 的内核 wheel（pip install vendor/*.whl），再跑这条命令"
    )


if __name__ == "__main__":
    argv = list(sys.argv[1:])
    if not any(arg.startswith("--registry") for arg in argv):
        argv = ["--registry", str(REGISTRY), *argv]
    if not any(
        arg in {"--list", "--all"} or arg.startswith("--adapter") for arg in argv
    ):
        argv.append("--all")
    raise SystemExit(main(argv))
