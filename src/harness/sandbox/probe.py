"""Sandbox isolation probe (PRD 3 `harness sandbox probe` / `doctor`).

Runs a fixed, harness-owned script inside a fresh container created with the
exact action settings and reports each isolation property as observed from
inside. Nothing from a target repository is mounted.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .docker_backend import DockerBackend
from .interface import ContainerLimits, ContainerSpec, Mount
from .runtime_profile import RuntimeProfileResolver

PROBE_SCRIPT = r'''
import json, os, socket
r = {}
r["uid"] = os.getuid(); r["gid"] = os.getgid()
status = open("/proc/self/status").read()
cap = [l.split()[1] for l in status.splitlines() if l.startswith("CapEff:")]
r["cap_eff"] = cap[0] if cap else None
nnp = [l.split()[1] for l in status.splitlines() if l.startswith("NoNewPrivs:")]
r["no_new_privs"] = nnp[0] if nnp else None
def can_write(path):
    try:
        with open(path, "w") as f:
            f.write("x")
        os.unlink(path)
        return True
    except OSError:
        return False
r["root_writable"] = can_write("/probe-root")
r["usr_writable"] = can_write("/usr/probe")
r["workspace_writable"] = can_write("/workspace/.probe")
r["context_writable"] = can_write("/context/.probe")
r["tmp_writable"] = can_write("/tmp/.probe")
def net():
    try:
        s = socket.create_connection(("1.1.1.1", 53), timeout=2); s.close(); return True
    except OSError:
        return False
r["network_egress"] = net()
up = []
try:
    for name in sorted(os.listdir("/sys/class/net")):
        try:
            state = open(f"/sys/class/net/{name}/operstate").read().strip()
        except OSError:
            continue
        if name != "lo" and state not in ("down", "notpresent", "lowerlayerdown"):
            up.append(name)
except OSError:
    pass
r["interfaces"] = up
try:
    r["routes"] = max(0, len(open("/proc/net/route").read().strip().splitlines()) - 1)
except OSError:
    r["routes"] = 0
r["docker_socket"] = any(os.path.exists(p) for p in ("/var/run/docker.sock", "/run/docker.sock"))
secret_names = ("AI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "DOCKER_HOST")
r["secret_env"] = sorted(k for k in os.environ if k in secret_names)
r["home"] = os.environ.get("HOME")
mounts = []
for line in open("/proc/self/mountinfo"):
    parts = line.split()
    if len(parts) > 4:
        mounts.append(parts[4])
r["mounts"] = sorted(set(m for m in mounts if m.startswith(("/workspace", "/context", "/output", "/home", "/Users", "/root", "/var/run", "/deps"))))
def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None
r["pids_max"] = read("/sys/fs/cgroup/pids.max")
r["memory_max"] = read("/sys/fs/cgroup/memory.max")
print(json.dumps(r))
'''


@dataclass
class ProbeCheck:
    name: str
    passed: bool
    observed: Any

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "observed": self.observed}


def run_probe(backend: DockerBackend, resolver: RuntimeProfileResolver, scratch_root: Path) -> Dict[str, Any]:
    runtime = resolver.resolve()
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(scratch_root), prefix="probe-") as temp:
        root = Path(temp)
        workspace, context, output = root / "workspace", root / "context", root / "output"
        for path in (workspace, context, output):
            path.mkdir()
        (context / "probe.py").write_text(PROBE_SCRIPT, encoding="utf-8")
        os.chmod(context, 0o555)
        spec = ContainerSpec(
            name=f"dobby-probe-{uuid.uuid4().hex[:12]}",
            image_id=runtime.image_id,
            command=("python", "-I", "-B", "/context/probe.py"),
            user=runtime.user,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
            mounts=(Mount(workspace, "/workspace", False), Mount(context, "/context", True), Mount(output, "/output", False)),
            limits=ContainerLimits(
                cpus=runtime.limits.cpus,
                memory_bytes=runtime.limits.memory_bytes,
                pids=runtime.limits.pids,
                wall_seconds=60,
                stdout_bytes=1024 * 1024,
                stderr_bytes=1024 * 1024,
                scratch_bytes=runtime.limits.scratch_bytes,
            ),
            labels={"org.dobby.harness": "1", "org.dobby.kind": "probe"},
        )
        outcome = backend.run(spec, stdout_path=root / "stdout", stderr_path=root / "stderr")
        stdout = (root / "stdout").read_bytes().decode("utf-8", "replace")
        stderr = (root / "stderr").read_bytes().decode("utf-8", "replace")
        os.chmod(context, 0o755)
    try:
        observed = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "PROBE_OUTPUT_UNPARSABLE", "stderr": stderr[-2000:], "exit_code": outcome.exit_code}
    checks: List[ProbeCheck] = [
        ProbeCheck("non_root_user", observed["uid"] != 0, observed["uid"]),
        ProbeCheck("no_effective_capabilities", observed["cap_eff"] in ("0000000000000000",), observed["cap_eff"]),
        ProbeCheck("no_new_privileges", observed["no_new_privs"] == "1", observed["no_new_privs"]),
        ProbeCheck("read_only_root_filesystem", not observed["root_writable"] and not observed["usr_writable"],
                   {"/": observed["root_writable"], "/usr": observed["usr_writable"]}),
        ProbeCheck("workspace_writable", observed["workspace_writable"], observed["workspace_writable"]),
        ProbeCheck("context_read_only", not observed["context_writable"], observed["context_writable"]),
        ProbeCheck("tmpfs_scratch", observed["tmp_writable"], observed["tmp_writable"]),
        ProbeCheck("network_disabled", not observed["network_egress"] and not observed["interfaces"] and observed["routes"] == 0,
                   {"egress": observed["network_egress"], "up_interfaces": observed["interfaces"], "routes": observed["routes"]}),
        ProbeCheck("no_engine_socket", not observed["docker_socket"], observed["docker_socket"]),
        ProbeCheck("no_secrets_in_env", not observed["secret_env"], observed["secret_env"]),
        ProbeCheck("only_declared_mounts", all(m in ("/workspace", "/context", "/output") for m in observed["mounts"]), observed["mounts"]),
        ProbeCheck("pids_limited", observed["pids_max"] not in (None, "max"), observed["pids_max"]),
        ProbeCheck("memory_limited", observed["memory_max"] not in (None, "max"), observed["memory_max"]),
        ProbeCheck("container_removed", outcome.removed, outcome.removed),
    ]
    return {
        "ok": all(check.passed for check in checks),
        "image_id": runtime.image_id,
        "runtime_profile": runtime.contract.runtime_profile_id,
        "engine_version": outcome.engine_version,
        "elapsed_ms": outcome.elapsed_ms,
        "checks": [check.as_dict() for check in checks],
    }
