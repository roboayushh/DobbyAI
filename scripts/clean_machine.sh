#!/usr/bin/env bash
# Clean-machine qualification (PRD 6 REL-030, gate setup_clean_machine).
#
# Copies the harness source (tracked + untracked, non-ignored files only: no .venv,
# data/, caches, or release evidence) into a fresh Debian python:3.12 container and runs
#   make setup  ->  make test  ->  make run INPUT=request.json   (headless)
# exactly as documented. The container talks to the host Docker engine, so the work
# directory is mounted at the SAME absolute path (sandbox bind mounts resolve on the host).
#
# The headless run needs a model: set AI_API_KEY and HARNESS_MODEL_PROFILE (deepseek, qwen,
# ...). For local development the claude-bridge profile is reached through a loopback
# forwarder inside the container to host.docker.internal:8765.
#
#   AI_API_KEY=... HARNESS_MODEL_PROFILE=deepseek scripts/clean_machine.sh [WORK_DIR]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${1:-$(mktemp -d "${TMPDIR:-/tmp}/harness-clean.XXXXXX")}"
WORK="$(cd "$WORK" && pwd -P)"
IMAGE="${CLEAN_IMAGE:-python:3.12-bookworm}"
PROFILE="${HARNESS_MODEL_PROFILE:-designated}"
EVIDENCE="$ROOT/release-evidence/clean-machine"
mkdir -p "$WORK/src" "$WORK/bin" "$WORK/tmp" "$EVIDENCE"

echo "== copying harness source into $WORK/src"
(cd "$ROOT" && git ls-files -co --exclude-standard -z | tar --null -T - --exclude 'release-evidence/*' -cf -) | tar -C "$WORK/src" -xf -

echo "== docker CLI for the clean container"
docker run --rm -v "$WORK/bin:/out" --entrypoint cp docker:27-cli /usr/local/bin/docker /out/docker

# A tiny buggy project for the headless run (git repository, one failing test).
mkdir -p "$WORK/case/src" "$WORK/case/tests" "$WORK/out"
printf 'def add(a, b):\n    return a - b\n' > "$WORK/case/src/calc.py"
: > "$WORK/case/src/__init__.py"
printf 'from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n' > "$WORK/case/tests/test_calc.py"
git -C "$WORK/case" init -q && git -C "$WORK/case" add -A && \
  git -C "$WORK/case" -c user.name=clean -c user.email=clean@example.invalid commit -qm baseline
cat > "$WORK/request.json" <<JSON
{"schema_version": "1.0", "request_id": "ereq_clean_machine", "adapter": {"name": "native_json_v1", "version": "1.0.0"},
 "repository": {"kind": "local_git", "locator": "$WORK/case"}, "task_mode": "single_issue", "execution_mode": "evaluation",
 "task": {"source_type": "direct_text", "text": "add(2, 3) returns -1 instead of 5; tests/test_calc.py::test_add fails. Make add return the sum."},
 "model_config_ref": "$PROFILE", "budgets": {"model_calls": 20, "wall_seconds": 900},
 "result_path": "$WORK/out/result.json", "export_path": "$WORK/out/bundle", "requested_effects": ["EXPORT"],
 "idempotency_key": "clean-machine-0001"}
JSON

cat > "$WORK/inside.sh" <<'INSIDE'
set -uo pipefail
cd "$WORK/src"
export PATH="$WORK/bin:$PATH" TMPDIR="$WORK/tmp" DATA_DIR="$WORK/data"
if [ "$HARNESS_MODEL_PROFILE" = "claude-bridge" ]; then
  python3 - <<'FWD' &
import socket, threading
def pipe(a, b):
    try:
        while (data := a.recv(65536)):
            b.sendall(data)
    except OSError:
        pass
    finally:
        a.close(); b.close()
server = socket.socket(); server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 8765)); server.listen(16)
while True:
    client, _ = server.accept()
    upstream = socket.create_connection(("host.docker.internal", 8765))
    for x, y in ((client, upstream), (upstream, client)):
        threading.Thread(target=pipe, args=(x, y), daemon=True).start()
FWD
fi
step() { local name=$1; shift; local t0=$(date +%s); "$@" > "$WORK/$name.log" 2>&1; local rc=$?; echo "$name $rc $(( $(date +%s) - t0 ))" >> "$WORK/steps.txt"; return 0; }
: > "$WORK/steps.txt"
step setup make setup
step test make test
step run make run INPUT="$WORK/request.json"
INSIDE

echo "== running make setup / make test / make run in $IMAGE (this takes a while)"
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$WORK:$WORK" -e WORK="$WORK" \
  -e AI_API_KEY="${AI_API_KEY:-}" -e HARNESS_MODEL_PROFILE="$PROFILE" \
  --add-host=host.docker.internal:host-gateway \
  "$IMAGE" bash "$WORK/inside.sh"

python3 - "$WORK" "$EVIDENCE" "$IMAGE" "$PROFILE" <<'PY'
import json, platform, re, sys, datetime, pathlib
work, evidence, image, profile = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4]
steps = {}
for line in (work / "steps.txt").read_text().splitlines():
    name, rc, secs = line.split()
    steps[name] = {"exit_code": int(rc), "seconds": int(secs)}
test_log = (work / "test.log").read_text(errors="replace")
summary = next((l.strip("= ") for l in reversed(test_log.splitlines()) if re.search(r"\d+ passed", l)), "no pytest summary")
run_out = [l for l in (work / "run.log").read_text(errors="replace").splitlines() if l.startswith("{")]
result = json.loads(run_out[-1]) if run_out else {}
steps["test"]["summary"] = summary
steps["run"].update(status=result.get("status"), export=(result.get("export") or {}).get("status"))
ok = steps["setup"]["exit_code"] == 0 and steps["test"]["exit_code"] == 0 and result.get("status") == "PASS"
record = {"schema_version": "1.0", "status": "PASS" if ok else "FAIL", "environment": f"{image} (linux container, host {platform.machine()})",
          "model_profile": profile, "prescribed_model": profile not in ("claude-bridge", "designated"),
          "summary": f"setup rc={steps['setup']['exit_code']}; test: {summary}; headless run: {result.get('status')}",
          "steps": steps, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
name = f"clean-machine-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
(evidence / name).write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record, indent=2))
PY
