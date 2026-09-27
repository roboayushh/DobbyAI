"""Harness invariant check: compile (never execute) changed Python files.

Usage: python -I -B /opt/harness/checks/syntax_check.py /context/changed_paths.json
Prints one JSON object and exits 0 when every listed Python file compiles.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE = Path("/workspace")


def main(argv: list) -> int:
    paths = json.loads(Path(argv[1]).read_text(encoding="utf-8")) if len(argv) > 1 else []
    checked, errors = [], []
    for relative in paths:
        if not isinstance(relative, str) or not relative.endswith(".py") or ".." in relative.split("/"):
            continue
        target = WORKSPACE / relative
        if not target.is_file() or target.is_symlink():
            continue
        checked.append(relative)
        try:
            compile(target.read_bytes(), relative, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            errors.append({"path": relative, "line": getattr(exc, "lineno", None), "message": str(exc)[:300]})
    print(json.dumps({"checked": checked, "errors": errors}, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
