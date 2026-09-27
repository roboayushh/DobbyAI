"""PRD 3 section 14: dependency setup parsing and setup-scoped egress control."""
from __future__ import annotations

import json
import socket
import tempfile
import uuid
from pathlib import Path

import pytest

from harness.execution.dependencies import parse_requirements


def test_requirements_are_sanitized_and_never_install_the_project() -> None:
    files = {
        "requirements.txt": (
            b"# core\nrequests>=2.0  # http\n-e .\n.\n./vendor/pkg\ngit+https://github.com/a/b.git\n"
            b"https://example.com/x.whl\n--index-url https://evil.example/simple\n-r requirements-dev.txt\n"
            b"attrs==23.1.0 --hash=sha256:abc\nmyproject\n"
        ),
        "requirements-dev.txt": b"pytest-mock\n-r ../../etc/passwd\n",
        "pyproject.toml": (
            b'[project]\nname = "MyProject"\ndependencies = ["click>=8", "pkg @ file:///tmp/x"]\n'
            b'[project.optional-dependencies]\ntest = ["hypothesis"]\ndocs = ["sphinx"]\n'
        ),
    }
    requirements, sources = parse_requirements(files)
    assert requirements == ["requests>=2.0", "pytest-mock", "attrs==23.1.0", "click>=8", "hypothesis"]
    assert "myproject" not in [r.lower() for r in requirements]
    assert "sphinx" not in requirements
    assert set(sources) == {"requirements.txt", "requirements-dev.txt", "pyproject.toml"}


def test_poetry_and_setup_cfg_are_read_without_execution() -> None:
    files = {
        "pyproject.toml": b'[tool.poetry.dependencies]\npython = "^3.10"\nrich = "^13"\nlocal = {path = "../x"}\n'
                          b'[tool.poetry.group.dev.dependencies]\npytest-cov = "*"\n',
        "setup.cfg": b"[metadata]\nname = demo\n[options]\ninstall_requires =\n    pyyaml>=6\n    toml\npackages = find:\n",
    }
    requirements, _ = parse_requirements(files)
    assert requirements == ["rich", "pytest-cov", "pyyaml>=6", "toml"]


def test_no_manifests_means_no_environment() -> None:
    assert parse_requirements({}) == ([], [])


# ------------------------------------------------------------ Docker-backed
def _docker_and_runtime():
    from tests.test_prd345_e2e import _runtime_ready

    return _runtime_ready()


def _pypi_reachable() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


docker_only = pytest.mark.skipif(not _docker_and_runtime(), reason="Docker sandbox runtime unavailable")


@docker_only
def test_setup_network_enforces_destination_allowlist(tmp_path: Path) -> None:
    from harness.sandbox import ContainerLimits, ContainerSpec, DockerBackend, Mount, RuntimeProfileResolver

    backend = DockerBackend(allowed_mount_roots=[tmp_path])
    runtime = RuntimeProfileResolver(backend).resolve()
    suffix = uuid.uuid4().hex[:10]
    network, proxy = f"{DockerBackend.SETUP_NETWORK_PREFIX}{suffix}", f"dobby-proxy-{suffix}"
    labels = {"org.dobby.harness": "1", "org.dobby.kind": "dependency_setup"}
    context = tmp_path / "context"
    context.mkdir()
    (context / "probe.py").write_text(
        "import json, socket\n"
        "out = {}\n"
        "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=3).close(); out['direct'] = True\n"
        "except OSError:\n    out['direct'] = False\n"
        "def connect(target):\n"
        "    s = socket.create_connection(('harness-proxy', 3128), timeout=10)\n"
        "    s.sendall(f'CONNECT {target} HTTP/1.1\\r\\nHost: {target}\\r\\n\\r\\n'.encode())\n"
        "    line = s.recv(200).split(b'\\r\\n')[0].decode(); s.close(); return line\n"
        "out['denied'] = connect('example.com:443')\n"
        "out['denied_port'] = connect('pypi.org:22')\n"
        "print(json.dumps(out))\n",
        encoding="utf-8",
    )
    try:
        backend.network_create_internal(network, labels)
        backend.start_egress_proxy(proxy, runtime.image_id, runtime.user, ["pypi.org:443"], {**labels, "org.dobby.kind": "egress_proxy"})
        backend.network_connect(network, proxy, "harness-proxy")
        spec = ContainerSpec(
            name=f"dobby-setup-probe-{suffix}", image_id=runtime.image_id, command=("python", "-I", "/context/probe.py"),
            user=runtime.user, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            mounts=(Mount(context, "/context", True),),
            limits=ContainerLimits(cpus=1, memory_bytes=512 * 1024 * 1024, pids=64, wall_seconds=60,
                                   stdout_bytes=65536, stderr_bytes=65536, scratch_bytes=64 * 1024 * 1024),
            labels=labels, workdir="/tmp", network=network,
        )
        outcome = backend.run(spec, stdout_path=tmp_path / "out", stderr_path=tmp_path / "err")
        observed = json.loads((tmp_path / "out").read_text().strip().splitlines()[-1])
    finally:
        backend.force_remove(proxy)
        backend.network_remove(network)
    assert outcome.exit_code == 0, (tmp_path / "err").read_text()
    assert observed["direct"] is False  # internal network: no route out
    assert "403" in observed["denied"] and "403" in observed["denied_port"]


@docker_only
@pytest.mark.skipif(not _pypi_reachable(), reason="PyPI is not reachable from this host")
def test_third_party_dependency_repo_verifies_instead_of_blocking(tmp_path: Path) -> None:
    from tests.support.harness_fixtures import prepare_run
    from tests.support.scripted_model import ScriptedAdapter, code_step, complete_step, patch_action, plan_step, validator_step
    from tests.test_prd345_e2e import stack

    files = {
        "requirements.txt": "six==1.16.0\n-e .\n",
        "src/__init__.py": "",
        "src/calc.py": "import six\n\n\ndef add(a, b):\n    return a - b\n",
        "tests/test_calc.py": "import six\nfrom src.calc import add\n\n\ndef test_add():\n    assert six.PY3 and add(2, 3) == 5\n",
    }
    env = prepare_run(tmp_path, files, "add(2, 3) should return 5. See tests/test_calc.py::test_add")
    fix = patch_action("src/calc.py", "return a - b", "return a + b", "tests/test_calc.py")
    _, _, queue = stack(env, ScriptedAdapter([plan_step(), code_step(fix), complete_step(), validator_step()]))
    final = queue.run(env.run_id)
    assert final.status == "COMPLETED_ALL"
    with env.run_store.get_connection() as conn:
        states = {row[0] for row in conn.execute("SELECT state FROM h_dependency_environments")}
        baseline = {row[0] for row in conn.execute("SELECT status FROM h_baseline_runs")}
    assert states == {"READY"}
    assert "BLOCKED_ENVIRONMENT" not in baseline
