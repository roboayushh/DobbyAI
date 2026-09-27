"""Sandbox entry point: ``python -I -B /opt/harness/action_runner.py /context/execution-request.json``.

The runner verifies the staged request, code, and context manifest hashes,
executes the generated action once in a fresh interpreter namespace with the
reviewed tool library, and exits. It has no model adapter, no host database,
and no network. Its exit status and output are advisory evidence; the host
container supervisor and manifest scan are authoritative.

Exit codes: 0 action completed, 1 action raised, 96 protocol error,
97 integrity error.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Read-only, host-verified dependency environment (PRD 3 section 14); appended so
# it can never shadow the harness tool library.
if os.environ.get("HARNESS_DEPENDENCY_SITE") and os.path.isdir(os.environ["HARNESS_DEPENDENCY_SITE"]):
    sys.path.append(os.environ["HARNESS_DEPENDENCY_SITE"])
for _root in [r for r in os.environ.get("HARNESS_SOURCE_ROOTS", "").split(":") if r and os.path.isdir(r)]:
    sys.path.append(_root)

EXIT_OK = 0
EXIT_ACTION_ERROR = 1
EXIT_PROTOCOL = 96
EXIT_INTEGRITY = 97


def _fail(code: int, message: str) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(f"HARNESS_WORKER_ERROR {message}\n")
    sys.stderr.flush()
    raise SystemExit(code)


def _load(path: str, limit: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        _fail(EXIT_PROTOCOL, f"cannot read {path}: {exc}")
    if len(data) > limit:
        _fail(EXIT_PROTOCOL, f"{path} exceeds {limit} bytes")
    return data


def main(argv: list) -> int:
    if len(argv) != 2:
        _fail(EXIT_PROTOCOL, "usage: action_runner.py EXECUTION_REQUEST")
    raw = _load(argv[1], 1024 * 1024)
    try:
        request = json.loads(raw)
    except ValueError:
        _fail(EXIT_PROTOCOL, "execution request is not JSON")
    if not isinstance(request, dict) or request.get("schema_version") != "1.0":
        _fail(EXIT_PROTOCOL, "unsupported execution request schema")
    code_ref = request.get("code") or {}
    manifest_ref = request.get("context_manifest") or {}
    code_bytes = _load(str(code_ref.get("container_path", "")), 256 * 1024)
    if hashlib.sha256(code_bytes).hexdigest() != code_ref.get("sha256"):
        _fail(EXIT_INTEGRITY, "action code hash mismatch")
    manifest_bytes = _load(str(manifest_ref.get("container_path", "")), 4 * 1024 * 1024)
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_ref.get("sha256"):
        _fail(EXIT_INTEGRITY, "context manifest hash mismatch")

    import harness_tools  # noqa: E402  (after sys.path setup)

    toolbox = harness_tools.Toolbox(request)
    namespace = {"__name__": "__action__", "__builtins__": __builtins__}
    namespace.update(toolbox.exports())
    try:
        source = code_bytes.decode("utf-8")
        compiled = compile(source, "action.py", "exec", dont_inherit=True)
    except (UnicodeDecodeError, SyntaxError) as exc:
        sys.stderr.write(f"HARNESS_ACTION_SYNTAX_ERROR {exc}\n")
        return EXIT_ACTION_ERROR
    status = EXIT_OK
    try:
        exec(compiled, namespace)
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else EXIT_ACTION_ERROR
        if status not in (EXIT_OK, EXIT_ACTION_ERROR):
            status = EXIT_ACTION_ERROR
    except BaseException:
        traceback.print_exc(limit=20)
        status = EXIT_ACTION_ERROR
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    if toolbox.integrity_failure:
        sys.stderr.write(f"HARNESS_ARTIFACT_INTEGRITY_ERROR {toolbox.integrity_failure}\n")
        return EXIT_INTEGRITY
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
