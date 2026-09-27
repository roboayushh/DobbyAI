"""Import roots of a Python repository (host-side, deterministic).

A ``src/`` (or ``lib/``) layout keeps packages out of the repository root, so tests
import them only when the project is installed. The sandbox never installs the
project itself (a stale installed copy could shadow the model's edits); instead
the source roots are put on ``PYTHONPATH`` ahead of the dependency site.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

LAYOUT_DIRS = ("src", "lib")


def source_roots(root: Path) -> List[str]:
    roots: List[str] = []
    for name in LAYOUT_DIRS:
        base = Path(root) / name
        if not base.is_dir() or base.is_symlink() or (base / "__init__.py").exists():
            continue  # a package named src/ is imported from the root itself
        packages = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and (p / "__init__.py").is_file()]
        modules = [p for p in base.iterdir() if p.is_file() and p.suffix == ".py"]
        if packages or modules:
            roots.append(name)
    return roots
