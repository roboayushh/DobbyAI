"""Isolated, content-addressed dependency environments (PRD 3 section 14).

Setup is separate from coding: a dedicated container on an internal
(no-route) Docker network reaches only a harness-owned allowlist proxy to the
Python package index. Repository manifests are parsed on the host (never
executed); only index requirements are installed, never the project itself or
VCS/URL/local-path requirements. The result is hashed, frozen read-only, and
mounted read-only at ``/deps`` for actions and verification, which keep
``--network=none``. When scoped egress cannot be enforced the environment is
recorded as ``NETWORK_POLICY_UNAVAILABLE`` and checks report
``BLOCKED_ENVIRONMENT`` honestly; there is never a host fallback.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.persistence import ArtifactStore, RunStore, canonical_json
from harness.persistence.events import append_event_sql
from harness.sandbox import ContainerLimits, ContainerSpec, DockerBackend, Mount, SandboxError
from harness.workspace.task_workspace import TaskWorkspaceService

ADAPTER_VERSION = "pip-target-v1"
DEFAULT_INDEX_HOSTS = ("pypi.org:443", "files.pythonhosted.org:443")
REQUIREMENT_FILES = (
    "requirements.txt", "requirements-dev.txt", "requirements_dev.txt", "requirements-test.txt",
    "requirements_test.txt", "requirements-tests.txt", "test-requirements.txt", "dev-requirements.txt",
    "requirements/base.txt", "requirements/dev.txt", "requirements/test.txt", "requirements/tests.txt",
)
TEST_EXTRAS = ("test", "tests", "testing", "dev", "develop")
TEST_ONLY = ("test", "tests", "testing")


def _test_groups(groups) -> list:
    """Prefer dedicated test groups; fall back to dev groups (often linters/tooling) only when none exist."""
    names = [name for name in groups if name.lower() in TEST_ONLY]
    return names or [name for name in groups if name.lower() in TEST_EXTRAS]
SKIP_PREFIXES = ("-e", "--editable", ".", "/", "file:", "git+", "hg+", "svn+", "bzr+", "http://", "https://",
                 "-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--trusted-host", "-c", "--constraint",
                 "--no-binary", "--only-binary", "--pre", "--prefer-binary", "--use-feature")
NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
MAX_REQUIREMENTS = 300


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class DependencyEnvironment:
    environment_id: str
    state: str  # READY, FAILED, BLOCKED, NOT_NEEDED
    site_root: Optional[Path]  # directory mounted at /deps (contains site-packages)
    environment_sha256: Optional[str]
    failure_code: Optional[str] = None


NOT_NEEDED = DependencyEnvironment("none", "NOT_NEEDED", None, None)


def _clean_line(line: str) -> Optional[str]:
    text = line.split(" #", 1)[0].strip()
    if not text or text.startswith("#"):
        return None
    text = re.sub(r"\s--hash[= ]\S+", "", text).strip()
    if text.startswith(SKIP_PREFIXES) or "://" in text or " @ " in text:
        return None
    if text.startswith("-"):
        return None
    return text


def parse_requirements(files: Dict[str, bytes], project_name: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Return (requirements, source manifests) from repository manifest bytes (no execution)."""
    requirements: List[str] = []
    sources: List[str] = []
    pyproject: Dict[str, Any] = {}
    if "pyproject.toml" in files:
        try:
            pyproject = tomllib.loads(files["pyproject.toml"].decode("utf-8", "replace"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            pyproject = {}
    declared_name = project_name or (pyproject.get("project") or {}).get("name") or ((pyproject.get("tool") or {}).get("poetry") or {}).get("name")
    if not declared_name and "setup.cfg" in files:
        found = re.search(r"(?m)^name\s*=\s*([A-Za-z0-9._-]+)\s*$", files["setup.cfg"].decode("utf-8", "replace"))
        declared_name = found.group(1) if found else None
    own = _canonical(declared_name) if isinstance(declared_name, str) and declared_name else None

    def add(requirement: str, source: str) -> None:
        cleaned = _clean_line(requirement)
        if cleaned is None:
            return
        match = NAME.match(cleaned)
        if not match or (own and _canonical(match.group(1)) == own):
            return
        if cleaned not in requirements and len(requirements) < MAX_REQUIREMENTS:
            requirements.append(cleaned)
            if source not in sources:
                sources.append(source)

    def read_requirements(path: str, depth: int = 0) -> None:
        data = files.get(path)
        if data is None or depth > 3:
            return
        for raw in data.decode("utf-8", "replace").splitlines():
            line = raw.strip()
            if line.startswith(("-r ", "--requirement ", "-r", "--requirement=")):
                target = re.split(r"[ =]", line, maxsplit=1)[-1].strip() if " " in line or "=" in line else line[2:].strip()
                joined = (PurePosixPath(path).parent / target).as_posix()
                if ".." not in PurePosixPath(joined).parts:
                    read_requirements(joined.lstrip("./"), depth + 1)
                continue
            add(line, path)

    for path in REQUIREMENT_FILES:
        read_requirements(path)
    if pyproject:
        data = pyproject
        project = data.get("project") or {}
        for item in project.get("dependencies") or []:
            if isinstance(item, str):
                add(item, "pyproject.toml")
        extras = project.get("optional-dependencies") or {}
        for extra in _test_groups(extras):
            for item in extras[extra] if isinstance(extras[extra], list) else []:
                if isinstance(item, str):
                    add(item, "pyproject.toml")
        groups = data.get("dependency-groups") or {}
        for group in _test_groups(groups):
            for item in groups[group] if isinstance(groups[group], list) else []:
                if isinstance(item, str):
                    add(item, "pyproject.toml")
        poetry = (data.get("tool") or {}).get("poetry") or {}
        for section in [poetry.get("dependencies") or {}] + [
            (group or {}).get("dependencies") or {}
            for name, group in ((poetry.get("group") or {}).items())
            if name.lower() in TEST_EXTRAS
        ] + [poetry.get("dev-dependencies") or {}]:
            for name, spec in section.items():
                if name.lower() == "python" or isinstance(spec, dict) and ("path" in spec or "git" in spec or "url" in spec):
                    continue
                add(name, "pyproject.toml")
    if "setup.cfg" in files:
        text = files["setup.cfg"].decode("utf-8", "replace")
        block = re.search(r"(?ms)^\[options\]\s*$(.*?)(?=^\[)", text + "\n[")
        if block:
            requires = re.search(r"(?ms)^install_requires\s*=\s*(.*?)(?=^\S)", block.group(1) + "\nx")
            if requires:
                for line in requires.group(1).splitlines():
                    add(line, "setup.cfg")
    return requirements, sources


class DependencyEnvironmentService:
    def __init__(
        self,
        *,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        data_root: Path,
        backend: DockerBackend,
        runtime_resolver: Any,
        workspaces: TaskWorkspaceService,
        enabled: bool = True,
        index_hosts: Sequence[str] = DEFAULT_INDEX_HOSTS,
        setup_seconds: int = 900,
        runtime_row: Any = None,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.root = Path(data_root).resolve() / "deps"
        self.backend = backend
        self.runtime_resolver = runtime_resolver
        self.workspaces = workspaces
        self.enabled = enabled
        self.index_hosts = tuple(sorted(index_hosts))
        self.setup_seconds = setup_seconds
        self.runtime_row = runtime_row  # callable(run_id) -> h_runtime_profiles row id
        self._memo: Dict[Tuple[str, str, str], DependencyEnvironment] = {}

    # --------------------------------------------------------------- facts
    def manifests(self, run_id: str, commit: str) -> Tuple[Dict[str, bytes], Optional[str]]:
        git = self.workspaces.git(run_id)
        wanted = set(REQUIREMENT_FILES) | {"pyproject.toml", "setup.cfg"}
        entries = [e for e in git.ls_tree(git.commit_tree_of(commit)) if e.object_type == "blob"]
        chosen = [e for e in entries if e.path in wanted or (e.path.startswith("requirements/") and e.path.endswith(".txt"))]
        blobs = git.cat_blobs(e.oid for e in chosen)
        files = {e.path: blobs[e.oid] for e in chosen if len(blobs[e.oid]) <= 512 * 1024}
        return files, None

    def policy_fingerprint(self) -> str:
        return _sha({"mode": "setup_scoped", "proxy": "egress_proxy@1", "allow": list(self.index_hosts)})

    def cache_key(self, runtime: Any, requirements: Sequence[str]) -> str:
        return _sha({
            "image_digest": runtime.contract.image_digest,
            "architecture": runtime.architecture,
            "adapter": ADAPTER_VERSION,
            "requirements": list(requirements),
            "policy": self.policy_fingerprint(),
        })

    # ---------------------------------------------------------------- API
    def ensure(self, run_id: str, task_id: str, commit: str) -> DependencyEnvironment:
        memo_key = (run_id, task_id, commit)
        if memo_key in self._memo:
            return self._memo[memo_key]
        files, _ = self.manifests(run_id, commit)
        requirements, sources = parse_requirements(files)
        if not requirements:
            self._memo[memo_key] = NOT_NEEDED
            return NOT_NEEDED
        runtime = self.runtime_resolver.resolve()
        key = self.cache_key(runtime, requirements)
        manifest_sha = _sha({path: hashlib.sha256(files[path]).hexdigest() for path in sorted(sources)})
        env = self._existing(run_id, task_id, key)
        if env is None:
            env = self._build(run_id, task_id, commit, runtime, key, requirements, manifest_sha)
        self._memo[memo_key] = env
        return env

    def _env_dir(self, key: str) -> Path:
        return self.root / key

    def _existing(self, run_id: str, task_id: str, key: str) -> Optional[DependencyEnvironment]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_dependency_environments WHERE run_id = ? AND task_id = ? AND cache_key_sha256 = ?",
                (run_id, task_id, key),
            ).fetchone()
        directory = self._env_dir(key)
        marker = directory / ".dobby-ready.json"
        if row and row["state"] in ("FAILED", "BLOCKED"):
            return DependencyEnvironment(row["dependency_environment_id"], row["state"], None, None, row["failure_code"])
        if marker.is_file():
            try:
                ready = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                return None
            if row is None:
                row_id = self._insert(run_id, task_id, None, key, ready.get("manifest_sha256", "0" * 64), "READY",
                                      environment_sha=ready["environment_sha256"])
            else:
                row_id = row["dependency_environment_id"]
            return DependencyEnvironment(row_id, "READY", directory, ready["environment_sha256"])
        return None

    def _insert(self, run_id: str, task_id: str, commit: Optional[str], key: str, manifest_sha: str, state: str,
                *, environment_sha: Optional[str] = None, failure: Optional[str] = None, log_artifact: Optional[str] = None) -> str:
        env_id = f"denv_{uuid.uuid4().hex[:16]}"
        profile_row = self.runtime_row(run_id) if self.runtime_row is not None else None
        with self.run_store.get_connection() as conn:
            with conn:
                if profile_row is None:
                    found = conn.execute(
                        "SELECT runtime_profile_row_id FROM h_runtime_profiles WHERE run_id = ? ORDER BY created_at DESC LIMIT 1", (run_id,)
                    ).fetchone()
                    if found is None:
                        raise RuntimeError("Runtime profile row is required before recording a dependency environment")
                    profile_row = found["runtime_profile_row_id"]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO h_dependency_environments(
                        dependency_environment_id, run_id, task_id, runtime_profile_row_id, source_revision,
                        manifest_sha256, lockfile_sha256, setup_policy_sha256, cache_key_sha256, environment_sha256,
                        environment_relpath, setup_log_artifact_id, state, failure_code, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (env_id, run_id, task_id, profile_row, commit or "cached", manifest_sha,
                     self.policy_fingerprint(), key, environment_sha, f"deps/{key}", log_artifact, state, failure, _now(), _now()),
                )
                existing = conn.execute(
                    "SELECT dependency_environment_id FROM h_dependency_environments WHERE run_id = ? AND task_id = ? AND cache_key_sha256 = ?",
                    (run_id, task_id, key),
                ).fetchone()
                env_id = existing["dependency_environment_id"]
                append_event_sql(conn, run_id, "DEPENDENCY_ENVIRONMENT_" + state, {
                    "task_id": task_id, "cache_key": key, "failure_code": failure, "environment_sha256": environment_sha,
                }, "PRD3", "PRD3", _now())
        return env_id

    def _build(self, run_id: str, task_id: str, commit: str, runtime: Any, key: str,
               requirements: Sequence[str], manifest_sha: str) -> DependencyEnvironment:
        if not self.enabled:
            env_id = self._insert(run_id, task_id, commit, key, manifest_sha, "BLOCKED", failure="DEPENDENCY_SETUP_DISABLED")
            return DependencyEnvironment(env_id, "BLOCKED", None, None, "DEPENDENCY_SETUP_DISABLED")
        self.root.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex[:12]
        work = self.root / f".build-{key[:16]}-{suffix}"
        env_dir = work / "env"
        context = work / "context"
        logs = work / "logs"
        for directory in (env_dir / "site-packages", context, logs):
            directory.mkdir(parents=True, exist_ok=True)
        (context / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
        os.chmod(context, 0o555)
        network = f"{DockerBackend.SETUP_NETWORK_PREFIX}{suffix}"
        proxy = f"dobby-proxy-{suffix}"
        labels = {"org.dobby.harness": "1", "org.dobby.kind": "dependency_setup", "org.dobby.run_id": run_id}
        failure: Optional[str] = None
        outcome = None
        proxy_log = ""
        try:
            try:
                self.backend.network_create_internal(network, labels)
                self.backend.start_egress_proxy(proxy, runtime.image_id, runtime.user, self.index_hosts,
                                                {**labels, "org.dobby.kind": "egress_proxy"})
                self.backend.network_connect(network, proxy, "harness-proxy")
            except SandboxError:
                failure = "NETWORK_POLICY_UNAVAILABLE"
            if failure is None:
                spec = ContainerSpec(
                    name=f"dobby-setup-{suffix}",
                    image_id=runtime.image_id,
                    command=("python", "-I", "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--no-cache-dir",
                             "--no-warn-script-location", "--progress-bar", "off", "--target", "/env/site-packages",
                             "-r", "/context/requirements.txt"),
                    user=runtime.user,
                    env={
                        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
                        "HTTPS_PROXY": "http://harness-proxy:3128", "HTTP_PROXY": "http://harness-proxy:3128",
                        "https_proxy": "http://harness-proxy:3128", "http_proxy": "http://harness-proxy:3128",
                        "PIP_INDEX_URL": "https://pypi.org/simple", "PIP_DEFAULT_TIMEOUT": "60",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    mounts=(Mount(context.resolve(), "/context", True), Mount(env_dir.resolve(), "/env", False)),
                    limits=ContainerLimits(
                        cpus=runtime.limits.cpus, memory_bytes=runtime.limits.memory_bytes, pids=runtime.limits.pids,
                        wall_seconds=self.setup_seconds, stdout_bytes=8 * 1024 * 1024, stderr_bytes=8 * 1024 * 1024,
                        scratch_bytes=runtime.limits.scratch_bytes, workspace_growth_bytes=6 * 1024 ** 3, new_files=400_000,
                    ),
                    labels={**labels, "org.dobby.cache_key": key[:32]},
                    workdir="/tmp",
                    network=network,
                    watch_workspace=env_dir,
                )
                outcome = self.backend.run(spec, stdout_path=logs / "stdout.log", stderr_path=logs / "stderr.log")
                if outcome.exit_code != 0 or outcome.timed_out or outcome.oom_killed or outcome.limit_breach:
                    failure = "DEPENDENCY_SETUP_FAILED"
        finally:
            try:
                proxy_log = self.backend.logs(proxy, 20_000)
            except Exception:
                proxy_log = ""
            self.backend.force_remove(proxy)
            self.backend.network_remove(network)
        log_text = ""
        for name in ("stdout.log", "stderr.log"):
            path = logs / name
            if path.is_file():
                log_text += f"--- {name} ---\n" + path.read_bytes()[-200_000:].decode("utf-8", "replace") + "\n"
        log_text += "--- egress proxy ---\n" + proxy_log
        log_path = f"prd3/dependencies/{task_id}/{key[:16]}-setup.log"
        self.artifact_store.write_bytes(run_id, log_path, log_text.encode("utf-8"), "text/plain", "runtime_setup_log", task_id)
        log_artifact = self.artifact_store.get_artifact_by_path(run_id, log_path)["artifact_id"]
        if failure:
            os.chmod(context, 0o755)
            shutil.rmtree(work, ignore_errors=True)
            env_id = self._insert(run_id, task_id, commit, key, manifest_sha, "FAILED", failure=failure, log_artifact=log_artifact)
            return DependencyEnvironment(env_id, "FAILED", None, None, failure)
        environment_sha = self._hash_tree(env_dir)
        final = self._env_dir(key)
        if final.exists():  # a concurrent build won; keep the first immutable result
            os.chmod(context, 0o755)
            shutil.rmtree(work, ignore_errors=True)
        else:
            (env_dir / ".dobby-ready.json").write_text(json.dumps({
                "environment_sha256": environment_sha, "manifest_sha256": manifest_sha, "requirements": list(requirements),
                "built_at": _now(),
            }), encoding="utf-8")
            os.replace(env_dir, final)
            os.chmod(context, 0o755)
            shutil.rmtree(work, ignore_errors=True)
            for current, dirs, files in os.walk(final):
                for name in files:
                    try:
                        os.chmod(os.path.join(current, name), 0o444)
                    except OSError:
                        pass
        ready = json.loads((final / ".dobby-ready.json").read_text(encoding="utf-8"))
        env_id = self._insert(run_id, task_id, commit, key, manifest_sha, "READY",
                              environment_sha=ready["environment_sha256"], log_artifact=log_artifact)
        return DependencyEnvironment(env_id, "READY", final, ready["environment_sha256"])

    @staticmethod
    def _hash_tree(root: Path) -> str:
        digest = hashlib.sha256()
        for current, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                path = Path(current) / name
                if path.is_symlink():
                    digest.update(f"L {path.relative_to(root).as_posix()} {os.readlink(path)}\n".encode())
                    continue
                with open(path, "rb") as handle:
                    file_hash = hashlib.sha256(handle.read()).hexdigest()
                digest.update(f"F {path.relative_to(root).as_posix()} {file_hash}\n".encode())
        return digest.hexdigest()
