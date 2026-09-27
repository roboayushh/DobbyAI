"""Docker sandbox backend (argument-array CLI; never a shell string).

Lifecycle per container: ``create`` -> ``inspect`` + verify every critical
setting (fail closed) -> ``start --attach`` with bounded stdout/stderr ->
watchdog enforces wall time, output bounds, and workspace growth -> ``inspect``
exit/OOM state -> ``rm -f`` and confirm removal. The action is not settled by
the caller until removal (or a confirmed stop) proves the container can no
longer mutate the workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from harness.persistence import canonical_json
from harness.sandbox.interface import (
    ContainerOutcome,
    ContainerSpec,
    SandboxCommunicationError,
    SandboxSettingsError,
    SandboxUnavailableError,
)

# Environment variables an image may contribute (the base Python image sets
# these). Anything else unexpected in the container environment fails closed.
IMAGE_ENV_ALLOWLIST = frozenset(
    {
        "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
        "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PIP_NO_CACHE_DIR",
        "PIP_DISABLE_PIP_VERSION_CHECK",
    }
)
SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "CREDENTIAL", "PRIVATE_KEY")
FORBIDDEN_MOUNT_SOURCES = ("/var/run/docker.sock", "/run/docker.sock", "/var/run/containerd", "/run/containerd")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class DockerBackend:
    name = "docker"

    def __init__(
        self,
        *,
        docker_bin: Optional[str] = None,
        allowed_mount_roots: Sequence[Path] = (),
        command_timeout: int = 60,
    ) -> None:
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"
        self.allowed_mount_roots = [Path(root).resolve() for root in allowed_mount_roots]
        self.command_timeout = command_timeout
        self._version: Optional[str] = None

    # ----------------------------------------------------------------- CLI
    def _docker(
        self,
        args: Sequence[str],
        *,
        timeout: Optional[int] = None,
        check: bool = True,
        input_bytes: Optional[bytes] = None,
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        # Only non-secret Docker client routing variables are forwarded.
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"):
            if key in os.environ:
                env[key] = os.environ[key]
        try:
            proc = subprocess.run(
                [self.docker_bin, *args],
                capture_output=True,
                timeout=timeout or self.command_timeout,
                env=env,
                input=input_bytes,
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailableError("Docker CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxCommunicationError(f"docker {args[0]} timed out") from exc
        if check and proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace")[:1000]
            if "Cannot connect to the Docker daemon" in message or "error during connect" in message:
                raise SandboxUnavailableError("Docker daemon is not reachable")
            raise SandboxCommunicationError(f"docker {args[0]} failed: {message.strip()}")
        return proc

    def availability(self) -> Tuple[bool, str, Optional[str]]:
        try:
            proc = self._docker(
                ["version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"],
                timeout=20,
                check=False,
            )
        except (SandboxUnavailableError, SandboxCommunicationError) as exc:
            return False, "", str(exc)
        if proc.returncode != 0:
            return False, "", proc.stderr.decode("utf-8", "replace").strip()[:500] or "Docker daemon unavailable"
        version = proc.stdout.decode().strip()
        self._version = version
        return True, version, None

    def engine_version(self) -> str:
        if self._version is None:
            ok, version, error = self.availability()
            if not ok:
                raise SandboxUnavailableError(error or "Docker unavailable")
        return self._version or "unknown"

    def info(self) -> Dict[str, Any]:
        proc = self._docker(["info", "--format", "{{json .}}"], timeout=30)
        return json.loads(proc.stdout)

    def image_inspect(self, reference: str) -> Optional[Dict[str, Any]]:
        proc = self._docker(["image", "inspect", reference], check=False, timeout=30)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return data[0] if data else None

    def build_image(self, context: Path, dockerfile: Path, tag: str, build_args: Dict[str, str], timeout: int = 1800) -> str:
        args = ["build", "--pull=false", "-f", str(dockerfile), "-t", tag]
        for key, value in sorted(build_args.items()):
            args.extend(["--build-arg", f"{key}={value}"])
        args.append(str(context))
        proc = self._docker(args, timeout=timeout, check=False)
        if proc.returncode != 0:
            raise SandboxCommunicationError(
                "Runtime image build failed: " + proc.stderr.decode("utf-8", "replace")[-2000:]
            )
        image = self.image_inspect(tag)
        if not image:
            raise SandboxCommunicationError("Built runtime image is not inspectable")
        return image["Id"]

    def find_by_labels(self, labels: Dict[str, str]) -> List[str]:
        args = ["ps", "-a", "--no-trunc", "--format", "{{.ID}}"]
        for key, value in sorted(labels.items()):
            args.extend(["--filter", f"label={key}={value}"])
        proc = self._docker(args, timeout=30)
        return [line.strip() for line in proc.stdout.decode().splitlines() if line.strip()]

    def container_state(self, container: str) -> Optional[Dict[str, Any]]:
        proc = self._docker(["inspect", container], check=False, timeout=30)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace")
            if "No such" in stderr or "no such" in stderr:
                return None
            raise SandboxCommunicationError(f"docker inspect failed: {stderr[:300]}")
        data = json.loads(proc.stdout)
        return data[0] if data else None

    def stop(self, container: str, grace: int = 2) -> None:
        self._docker(["stop", "-t", str(max(0, grace)), container], check=False, timeout=grace + 30)

    def kill(self, container: str) -> None:
        self._docker(["kill", "--signal", "KILL", container], check=False, timeout=30)

    def force_remove(self, container: str) -> bool:
        self._docker(["rm", "-f", "-v", container], check=False, timeout=60)
        return self.container_state(container) is None

    # ------------------------------------------------ setup-scoped network
    SETUP_NETWORK_PREFIX = "dobby-setup-"

    @classmethod
    def is_setup_network(cls, spec: ContainerSpec) -> bool:
        return spec.network.startswith(cls.SETUP_NETWORK_PREFIX) and spec.labels.get("org.dobby.kind") == "dependency_setup"

    def network_create_internal(self, name: str, labels: Dict[str, str]) -> None:
        if not name.startswith(self.SETUP_NETWORK_PREFIX):
            raise SandboxSettingsError("Harness networks must use the setup prefix")
        args = ["network", "create", "--internal", "--driver", "bridge"]
        for key, value in sorted(labels.items()):
            args.extend(["--label", f"{key}={value}"])
        self._docker([*args, name], timeout=60)

    def network_inspect(self, name: str) -> Optional[Dict[str, Any]]:
        proc = self._docker(["network", "inspect", name], check=False, timeout=30)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return data[0] if data else None

    def network_connect(self, network: str, container: str, alias: str) -> None:
        self._docker(["network", "connect", "--alias", alias, network, container], timeout=60)

    def network_remove(self, name: str) -> None:
        self._docker(["network", "rm", name], check=False, timeout=60)

    def start_egress_proxy(self, name: str, image_id: str, user: str, allow: Sequence[str], labels: Dict[str, str]) -> str:
        """Locked-down CONNECT allowlist proxy on the default bridge (the only egress path for setup)."""
        args = [
            "run", "-d", "--name", name, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=64", "--memory=268435456", "--memory-swap=268435456", "--cpus=1", "--init",
            f"--user={user}", "--tmpfs=/tmp:rw,nosuid,nodev,size=16m", "--network=bridge",
        ]
        for key, value in sorted(labels.items()):
            args.append(f"--label={key}={value}")
        args.extend([image_id, "python", "-I", "-B", "/opt/harness/egress_proxy.py", "--listen", "0.0.0.0:3128"])
        for item in allow:
            args.extend(["--allow", item])
        return self._docker(args, timeout=120).stdout.decode().strip()

    def logs(self, container: str, limit: int = 200_000) -> str:
        proc = self._docker(["logs", container], check=False, timeout=30)
        return (proc.stdout + proc.stderr).decode("utf-8", "replace")[-limit:]

    # --------------------------------------------------------- create args
    def create_args(self, spec: ContainerSpec) -> List[str]:
        limits = spec.limits
        if spec.network != "none" and not self.is_setup_network(spec):
            raise SandboxSettingsError("Only network mode 'none' is supported for sandbox containers")
        if spec.network != "none":
            info = self.network_inspect(spec.network)
            if not info or info.get("Internal") is not True:
                raise SandboxSettingsError("Setup network must be an existing internal (no-route) network")
        uid = spec.user.split(":", 1)[0]
        if not uid.isdigit() or int(uid) == 0:
            raise SandboxSettingsError("Sandbox containers must run as a numeric non-root UID")
        args = [
            "create",
            "--name", spec.name,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--pids-limit={limits.pids}",
            f"--memory={limits.memory_bytes}",
            f"--memory-swap={limits.memory_bytes}",
            f"--cpus={limits.cpus}",
            f"--network={spec.network}",
            "--ipc=private",
            "--shm-size=64m",
            "--cgroupns=private",
            "--init",
            "--log-driver=none",
            f"--user={spec.user}",
            f"--ulimit=nofile={limits.open_files}:{limits.open_files}",
            f"--ulimit=fsize={limits.max_file_bytes}:{limits.max_file_bytes}",
            "--ulimit=core=0:0",
            f"--stop-timeout={limits.stop_grace_seconds}",
            f"--workdir={spec.workdir}",
        ]
        for mount in spec.mounts:
            source = self._checked_mount_source(mount.source)
            option = f"type=bind,src={source},dst={mount.target}"
            if mount.read_only:
                option += ",readonly"
            args.append(f"--mount={option}")
        tmpfs = dict(spec.tmpfs) or {
            "/tmp": f"rw,nosuid,nodev,size={limits.scratch_bytes}",
            "/run/harness": "rw,nosuid,nodev,size=16m",
        }
        for target, options in sorted(tmpfs.items()):
            args.append(f"--tmpfs={target}:{options}")
        for key, value in sorted(spec.env.items()):
            args.append(f"--env={key}={value}")
        for key, value in sorted(spec.labels.items()):
            args.append(f"--label={key}={value}")
        args.append(spec.image_id)
        args.extend(spec.command)
        return args

    def _checked_mount_source(self, source: Path) -> str:
        resolved = Path(source).resolve()
        text = str(resolved)
        if any(text.startswith(marker) for marker in FORBIDDEN_MOUNT_SOURCES) or any(ch in text for ch in ",\"\n="):
            raise SandboxSettingsError(f"Forbidden mount source: {text}")
        if not resolved.is_dir():
            raise SandboxSettingsError(f"Mount source is not a directory: {text}")
        if self.allowed_mount_roots and not any(
            _is_within(resolved, root) for root in self.allowed_mount_roots
        ):
            raise SandboxSettingsError(f"Mount source outside registered roots: {text}")
        return text

    # ------------------------------------------------------ verification
    def verify_settings(self, spec: ContainerSpec, inspected: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
        """Return (violations, security-relevant settings) for a created container."""
        host = inspected.get("HostConfig") or {}
        config = inspected.get("Config") or {}
        violations: List[str] = []

        def expect(condition: bool, message: str) -> None:
            if not condition:
                violations.append(message)

        security = [str(item) for item in host.get("SecurityOpt") or []]
        expect(host.get("ReadonlyRootfs") is True, "root filesystem is not read-only")
        expect([c.upper() for c in host.get("CapDrop") or []] in (["ALL"], ["CAP_ALL"]), "capabilities not dropped")
        expect(not host.get("CapAdd"), "capabilities were added")
        expect(host.get("Privileged") is False, "privileged mode enabled")
        expect(any(item.startswith("no-new-privileges") for item in security), "no-new-privileges missing")
        expect(not any("unconfined" in item or item == "label=disable" for item in security), "seccomp/apparmor disabled")
        if spec.network == "none":
            expect(host.get("NetworkMode") == "none", "network is not 'none'")
        else:
            expect(self.is_setup_network(spec) and host.get("NetworkMode") == spec.network, "setup network differs")
            info = self.network_inspect(spec.network) or {}
            expect(info.get("Internal") is True, "setup network is not internal")
        expect(host.get("PidMode", "") in ("", "private"), "host PID namespace shared")
        expect(host.get("IpcMode") in ("private", "none"), "host IPC namespace shared")
        expect(host.get("UTSMode", "") in ("", "private"), "host UTS namespace shared")
        expect(host.get("UsernsMode", "") in ("", "private"), "host user namespace shared")
        expect(host.get("CgroupnsMode", "private") != "host", "host cgroup namespace shared")
        expect(host.get("PidsLimit") == spec.limits.pids, "PID limit differs")
        expect(host.get("Memory") == spec.limits.memory_bytes, "memory limit differs")
        expect(host.get("MemorySwap") == spec.limits.memory_bytes, "swap is not disabled")
        expect(host.get("NanoCpus") == int(spec.limits.cpus * 1e9), "CPU limit differs")
        expect(not host.get("Devices"), "devices were added")
        expect(not host.get("DeviceRequests"), "device requests present")
        expect(not host.get("Binds"), "legacy binds present")
        expect(not host.get("VolumesFrom"), "volumes-from present")
        expect(not host.get("ExtraHosts"), "extra hosts present")
        expect(host.get("Init") is True, "init process disabled")
        expect((host.get("LogConfig") or {}).get("Type") == "none", "logging driver would retain output")
        ulimits = {item.get("Name"): item for item in host.get("Ulimits") or []}
        expect("nofile" in ulimits and "fsize" in ulimits, "resource ulimits missing")
        mounts = sorted(
            (m.get("Source"), m.get("Target"), bool(m.get("ReadOnly")), m.get("Type"))
            for m in host.get("Mounts") or []
        )
        wanted = sorted(
            (str(Path(m.source).resolve()), m.target, m.read_only, "bind") for m in spec.mounts
        )
        expect(mounts == wanted, "mount set differs from the requested mounts")
        for source, _, _, _ in mounts:
            expect(not any(str(source).startswith(marker) for marker in FORBIDDEN_MOUNT_SOURCES), "engine socket mounted")
        user = str(config.get("User", ""))
        expect(user == spec.user and user.split(":")[0] not in ("", "0", "root"), "container user is not the non-root spec user")
        expect(inspected.get("Image") == spec.image_id, "container image differs from the pinned image ID")
        env_items = {}
        for item in config.get("Env") or []:
            key, _, value = str(item).partition("=")
            env_items[key] = value
        for key, value in spec.env.items():
            expect(env_items.get(key) == value, f"environment value differs for {key}")
        for key in env_items:
            if key not in spec.env:
                expect(key in IMAGE_ENV_ALLOWLIST, f"unexpected environment variable {key}")
            expect(not any(marker in key.upper() for marker in SECRET_ENV_MARKERS), f"secret-like environment variable {key}")
        labels = config.get("Labels") or {}
        for key, value in spec.labels.items():
            expect(labels.get(key) == value, f"label differs: {key}")
        settings = {
            "read_only_root": host.get("ReadonlyRootfs"),
            "cap_drop": host.get("CapDrop"),
            "cap_add": host.get("CapAdd"),
            "privileged": host.get("Privileged"),
            "security_opt": sorted(security),
            "network": host.get("NetworkMode"),
            "pid_mode": host.get("PidMode"),
            "ipc_mode": host.get("IpcMode"),
            "uts_mode": host.get("UTSMode"),
            "userns_mode": host.get("UsernsMode"),
            "pids_limit": host.get("PidsLimit"),
            "memory": host.get("Memory"),
            "memory_swap": host.get("MemorySwap"),
            "nano_cpus": host.get("NanoCpus"),
            "init": host.get("Init"),
            "ulimits": sorted((k, v.get("Soft"), v.get("Hard")) for k, v in ulimits.items()),
            "mounts": [(t, ro) for _, t, ro, _ in mounts],
            "tmpfs": sorted((host.get("Tmpfs") or {}).keys()),
            "user": user,
            "image": inspected.get("Image"),
            "env_keys": sorted(env_items),
        }
        return violations, settings

    # ---------------------------------------------------------------- run
    def run(
        self,
        spec: ContainerSpec,
        *,
        stdout_path: Path,
        stderr_path: Path,
        cancel_event: Optional[threading.Event] = None,
        on_created: Optional[Callable[[str, str], None]] = None,
        on_started: Optional[Callable[[], None]] = None,
    ) -> ContainerOutcome:
        engine_version = self.engine_version()
        created = self._docker(self.create_args(spec), timeout=120)
        container_id = created.stdout.decode().strip()
        started_flag = False
        try:
            inspected = self.container_state(container_id)
            if inspected is None:
                raise SandboxCommunicationError("Created container disappeared before inspection")
            violations, settings = self.verify_settings(spec, inspected)
            if violations:
                raise SandboxSettingsError("Container settings failed verification: " + "; ".join(violations))
            settings_sha = _sha(settings)
            if on_created is not None:
                on_created(container_id, settings_sha)
            outcome = self._attach_and_supervise(
                spec, container_id, settings_sha, engine_version, stdout_path, stderr_path, cancel_event, on_started
            )
            started_flag = outcome.started
            return outcome
        except BaseException:
            if not started_flag:
                self.force_remove(container_id)
            raise

    def _attach_and_supervise(
        self,
        spec: ContainerSpec,
        container_id: str,
        settings_sha: str,
        engine_version: str,
        stdout_path: Path,
        stderr_path: Path,
        cancel_event: Optional[threading.Event],
        on_started: Optional[Callable[[], None]],
    ) -> ContainerOutcome:
        limits = spec.limits
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        state = {"breach": None, "stdout": 0, "stderr": 0, "output_limit": False}
        stop_requested = threading.Event()

        def request_stop(reason: str) -> None:
            if state["breach"] is None:
                state["breach"] = reason
            stop_requested.set()

        attach = subprocess.Popen(
            [self.docker_bin, "start", "--attach", container_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
                 **{k: os.environ[k] for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG") if k in os.environ}},
        )
        begin = time.monotonic()
        if on_started is not None:
            on_started()

        def pump(stream, path: Path, key: str, cap: int) -> None:
            with open(path, "wb") as sink:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    remaining = cap - state[key]
                    if remaining > 0:
                        sink.write(chunk[:remaining])
                    state[key] += len(chunk)
                    if state[key] > cap:
                        state["output_limit"] = True
                        request_stop(f"{key.upper()}_LIMIT")

        readers = [
            threading.Thread(target=pump, args=(attach.stdout, stdout_path, "stdout", limits.stdout_bytes), daemon=True),
            threading.Thread(target=pump, args=(attach.stderr, stderr_path, "stderr", limits.stderr_bytes), daemon=True),
        ]
        for reader in readers:
            reader.start()

        baseline = _usage(spec.watch_workspace) if spec.watch_workspace else (0, 0)
        timed_out = False
        cancelled = False
        last_probe = 0.0
        while attach.poll() is None:
            now = time.monotonic()
            if now - begin > limits.wall_seconds:
                timed_out = True
                request_stop("WALL_TIME")
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                request_stop("CANCELLED")
            if now - last_probe >= 0.5:
                last_probe = now
                if spec.watch_workspace is not None and (limits.workspace_growth_bytes is not None or limits.new_files is not None):
                    size, files = _usage(spec.watch_workspace)
                    if limits.workspace_growth_bytes is not None and size - baseline[0] > limits.workspace_growth_bytes:
                        request_stop("WORKSPACE_GROWTH")
                    if limits.new_files is not None and files - baseline[1] > limits.new_files:
                        request_stop("NEW_FILES")
                if spec.watch_output is not None:
                    size, _ = _usage(spec.watch_output)
                    if size > limits.output_dir_bytes:
                        request_stop("OUTPUT_DIR_LIMIT")
            if stop_requested.is_set():
                break
            time.sleep(0.05)

        if stop_requested.is_set():
            if state["breach"] in ("WALL_TIME", "CANCELLED"):
                self.stop(container_id, limits.stop_grace_seconds)
            else:
                self.kill(container_id)
        try:
            attach.wait(timeout=limits.stop_grace_seconds + 30)
        except subprocess.TimeoutExpired:
            self.kill(container_id)
            attach.kill()
            attach.wait(timeout=10)
        for reader in readers:
            reader.join(timeout=10)
        elapsed_ms = int((time.monotonic() - begin) * 1000)

        inspected = self.container_state(container_id)
        if inspected is None:
            raise SandboxCommunicationError("Container vanished before its outcome could be inspected")
        container_state = inspected.get("State") or {}
        if container_state.get("Running"):
            self.kill(container_id)
            time.sleep(0.5)
            inspected = self.container_state(container_id) or inspected
            container_state = inspected.get("State") or {}
        exit_code = container_state.get("ExitCode")
        oom = bool(container_state.get("OOMKilled"))
        signal_number = exit_code - 128 if isinstance(exit_code, int) and exit_code > 128 else None
        removed = self.force_remove(container_id)
        return ContainerOutcome(
            container_name=spec.name,
            container_id=container_id,
            exit_code=exit_code,
            signal=signal_number,
            oom_killed=oom,
            timed_out=timed_out,
            cancelled=cancelled,
            output_limit_exceeded=bool(state["output_limit"]) or state["breach"] == "OUTPUT_DIR_LIMIT",
            limit_breach=state["breach"],
            elapsed_ms=elapsed_ms,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_bytes=min(state["stdout"], limits.stdout_bytes),
            stderr_bytes=min(state["stderr"], limits.stderr_bytes),
            settings_sha256=settings_sha,
            engine_version=engine_version,
            removed=removed,
            started=True,
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _usage(root: Optional[Path]) -> Tuple[int, int]:
    """Bytes and regular-file count beneath ``root`` without following links."""
    if root is None:
        return 0, 0
    total = 0
    files = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def host_architecture() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
