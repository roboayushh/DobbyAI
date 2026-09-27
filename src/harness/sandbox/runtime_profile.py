"""Pinned runtime profile resolution (PRD 3 sections 8.1-8.2, 11.5).

The runtime image is built locally from ``runtime/python/Dockerfile`` whose
base image is pinned by digest. The resulting content-addressed image ID, the
hash of the reviewed worker/tool library, and the host architecture are
recorded in ``config/runtime.lock.json``. Every launch uses the image ID, never
a mutable tag, and resolution fails closed when the image, its labels, or the
host tool library differ from the lock.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from harness.contracts.execution import NetworkMode, RuntimeProfileV1
from harness.persistence import canonical_json
from harness.policy.limits import SandboxLimits
from harness.sandbox.docker_backend import DockerBackend, host_architecture
from harness.sandbox.interface import RuntimeImageInvalidError, SandboxUnavailableError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
DEFAULT_LOCK_PATH = PROJECT_ROOT / "config" / "runtime.lock.json"
DOCKERFILE = PROJECT_ROOT / "runtime" / "python" / "Dockerfile"
IMAGE_TAG = "dobby-harness-python:1.0.0"
RUNTIME_PROFILE_ID = "python-default"
SANDBOX_UID = 65532


def tool_library_sha256(worker_dir: Path = WORKER_DIR) -> str:
    """Hash of every reviewed worker file (path + bytes), excluding caches."""
    digest = hashlib.sha256()
    for path in sorted(worker_dir.rglob("*")):
        relative = path.relative_to(worker_dir).as_posix()
        if "__pycache__" in relative or path.suffix in (".pyc", ".pyo") or not path.is_file():
            continue
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


def worker_version(worker_dir: Path = WORKER_DIR) -> str:
    try:
        return (worker_dir / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def container_user() -> str:
    """Numeric non-root user for sandbox processes.

    Bind-mounted task workspaces are owned by the host user; running as that
    (non-root) UID lets Linux hosts write through the mount without widening
    file permissions. A root host falls back to the image's sandbox UID.
    """
    uid = os.getuid() if hasattr(os, "getuid") else SANDBOX_UID
    gid = os.getgid() if hasattr(os, "getgid") else SANDBOX_UID
    if uid == 0:
        return f"{SANDBOX_UID}:{SANDBOX_UID}"
    return f"{uid}:{gid}"


@dataclass(frozen=True)
class ResolvedRuntime:
    contract: RuntimeProfileV1
    image_id: str
    tool_library_sha256: str
    architecture: str
    limits: SandboxLimits
    user: str
    lock: Dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return self.contract.profile_fingerprint


class RuntimeProfileResolver:
    def __init__(
        self,
        backend: DockerBackend,
        *,
        lock_path: Path = DEFAULT_LOCK_PATH,
        limits: SandboxLimits = SandboxLimits(),
    ) -> None:
        self.backend = backend
        self.lock_path = Path(lock_path)
        self.limits = limits

    # ------------------------------------------------------------- build
    def build(self, *, context: Path = PROJECT_ROOT, dockerfile: Path = DOCKERFILE, tag: str = IMAGE_TAG) -> Dict[str, Any]:
        ok, version, error = self.backend.availability()
        if not ok:
            raise SandboxUnavailableError(error or "Docker is unavailable")
        tool_hash = tool_library_sha256()
        image_id = self.backend.build_image(
            context,
            dockerfile,
            tag,
            {"TOOL_LIBRARY_SHA256": tool_hash, "WORKER_VERSION": worker_version()},
        )
        image = self.backend.image_inspect(image_id) or {}
        lock = {
            "schema_version": "1.0",
            "runtime_profile_id": RUNTIME_PROFILE_ID,
            "runtime": "python",
            "runtime_version": "3.12",
            "image_tag": tag,
            "image_id": image_id,
            "image_digest": image_id.split(":", 1)[-1],
            "architecture": image.get("Architecture"),
            "tool_library_sha256": tool_hash,
            "worker_version": worker_version(),
            "tool_library_version": worker_version(),
            "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
            "engine_version": version,
            "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.lock_path.with_suffix(".tmp")
        temp.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, self.lock_path)
        return lock

    # ----------------------------------------------------------- resolve
    def load_lock(self) -> Dict[str, Any]:
        if not self.lock_path.is_file():
            raise RuntimeImageInvalidError(
                f"Runtime lock {self.lock_path} is missing; run `make runtime-image` (or `harness sandbox build`)"
            )
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeImageInvalidError("Runtime lock is not valid JSON") from exc
        for key in ("image_id", "image_digest", "tool_library_sha256", "runtime_profile_id", "architecture"):
            if not lock.get(key):
                raise RuntimeImageInvalidError(f"Runtime lock is missing {key}")
        if not str(lock["image_id"]).startswith("sha256:"):
            raise RuntimeImageInvalidError("Runtime lock must pin an immutable image ID, not a tag")
        return lock

    def resolve(self, profile_id: str = RUNTIME_PROFILE_ID, *, verify_image: bool = True) -> ResolvedRuntime:
        lock = self.load_lock()
        if lock["runtime_profile_id"] != profile_id:
            raise RuntimeImageInvalidError(f"Unknown runtime profile: {profile_id}")
        host_tools = tool_library_sha256()
        if host_tools != lock["tool_library_sha256"]:
            raise RuntimeImageInvalidError(
                "Runtime image carries a different worker/tool library than this harness; rebuild the runtime image"
            )
        if verify_image:
            ok, _, error = self.backend.availability()
            if not ok:
                raise SandboxUnavailableError(error or "Docker is unavailable")
            image = self.backend.image_inspect(lock["image_id"])
            if not image:
                raise RuntimeImageInvalidError(f"Pinned runtime image {lock['image_id']} is not present")
            if image.get("Id") != lock["image_id"]:
                raise RuntimeImageInvalidError("Pinned runtime image ID mismatch")
            labels = (image.get("Config") or {}).get("Labels") or {}
            if labels.get("org.dobby.tool_library_sha256") != lock["tool_library_sha256"]:
                raise RuntimeImageInvalidError("Runtime image tool-library label differs from the lock")
            if image.get("Architecture") != lock["architecture"]:
                raise RuntimeImageInvalidError("Runtime image architecture differs from the lock")
            if lock["architecture"] != host_architecture():
                raise RuntimeImageInvalidError(
                    f"Runtime image architecture {lock['architecture']} does not match host {host_architecture()}"
                )
        user = container_user()
        uid, gid = (int(part) for part in user.split(":"))
        fingerprint_payload = {
            "runtime_profile_id": profile_id,
            "image_digest": lock["image_digest"],
            "architecture": lock["architecture"],
            "tool_library_sha256": lock["tool_library_sha256"],
            "worker_version": lock.get("worker_version"),
            "limits_fingerprint": self.limits.fingerprint(),
            "network": NetworkMode.NONE.value,
            "user_policy": "host-non-root-or-sandbox-uid",
        }
        contract = RuntimeProfileV1(
            runtime_profile_id=profile_id,
            runtime_version=str(lock.get("runtime_version", "3.12")),
            image_reference=lock["image_id"],
            image_digest=lock["image_digest"],
            worker_version=str(lock.get("worker_version", "1.0.0")),
            tool_library_version=str(lock.get("tool_library_version", "1.0.0")),
            non_root_uid=uid,
            non_root_gid=gid,
            default_network_mode=NetworkMode.NONE,
            shell_path="/bin/bash",
            limits_profile="ordinary-v1",
            profile_fingerprint=hashlib.sha256(canonical_json(fingerprint_payload).encode()).hexdigest(),
        )
        return ResolvedRuntime(contract, lock["image_id"], host_tools, lock["architecture"], self.limits, user, lock)
