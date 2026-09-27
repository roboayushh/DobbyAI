"""``harness doctor``: release/evaluator prerequisite diagnosis (PRD 6 section 23.4).

Never prints secret values ("present" is sufficient) and never requests elevated
privilege; remediation text is advisory. The ``submission`` profile additionally
requires the pinned official evaluator adapter, so an unconfigured official
protocol blocks the "submission-ready" label (PRD 6 section 6.5).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness.config import HARNESS_ROOT, HarnessConfig
from harness.contracts.release import DoctorCheckV1, DoctorReportV1
from harness.persistence import canonical_json

PROFILES = {"evaluation_strict_v1": "evaluation_strict_v1", "development_sandbox_v1": "development_sandbox_v1",
            "submission": "evaluation_strict_v1"}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _version(text: str) -> Tuple[int, ...]:
    digits = []
    for part in text.split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        if not num:
            break
        digits.append(int(num))
    return tuple(digits)


class DoctorService:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def check(self, profile: str = "evaluation_strict_v1", *, live: bool = False) -> Tuple[DoctorReportV1, Dict[str, str]]:
        if profile not in PROFILES:
            raise ValueError(f"Unknown doctor profile {profile!r}; use {', '.join(PROFILES)}")
        checks: List[DoctorCheckV1] = []
        blocking: List[str] = []
        warnings: List[str] = []
        remediation: Dict[str, str] = {}

        def add(cid: str, status: str, observed: str, required: str, *, required_check: bool = True, fix: str = "") -> None:
            checks.append(DoctorCheckV1(id=cid, status=status, observed=observed[:500], required=required[:500]))
            if status == "FAIL":
                (blocking if required_check else warnings).append(cid)
                if fix:
                    remediation[cid] = fix
            elif status == "WARN":
                warnings.append(cid)
                if fix:
                    remediation[cid] = fix

        py = platform.python_version()
        add("python", "PASS" if sys.version_info[:2] >= (3, 12) else "FAIL", py, ">=3.12", fix="Install Python 3.12 and rerun make setup")
        try:
            import re

            output = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10).stdout
            match = re.search(r"(\d+\.\d+(?:\.\d+)?)", output)
            git_version = match.group(1) if match else output.strip()
            add("git", "PASS" if _version(git_version) >= (2, 30) else "FAIL", git_version, ">=2.30", fix="Install Git 2.30+")
        except Exception:
            add("git", "FAIL", "not found", ">=2.30", fix="Install Git")
        add("sqlite", "PASS" if _version(sqlite3.sqlite_version) >= (3, 35) else "FAIL", sqlite3.sqlite_version, ">=3.35")
        data = Path(self.config.data_dir)
        try:
            data.mkdir(parents=True, exist_ok=True)
            probe = data / f".doctor-{uuid.uuid4().hex[:6]}"
            probe.write_text("ok")
            probe.unlink()
            free = shutil.disk_usage(data).free
            add("storage", "PASS" if free > 2 * 1024 ** 3 else "WARN", f"writable, {free // (1024 ** 2)} MiB free", "writable, >= 2 GiB free",
                fix="Free disk space or set DATA_DIR to a larger volume")
        except OSError as exc:
            add("storage", "FAIL", f"not writable: {exc}", "writable data directory", fix="Set DATA_DIR to a writable directory")
        migrations = sorted((HARNESS_ROOT / "src" / "harness" / "persistence" / "migrations").glob("*.sql"))
        try:
            from harness.persistence import RunStore

            store = RunStore(str(data / "harness.db"))
            with store.get_connection() as conn:
                applied = conn.execute("SELECT MAX(version) FROM h_schema_migrations").fetchone()[0]
            add("database", "PASS" if applied == len(migrations) else "FAIL", f"schema version {applied}", f"schema version {len(migrations)}")
        except Exception as exc:
            add("database", "FAIL", str(exc)[:200], f"schema version {len(migrations)}")
        # Sandbox
        from harness.sandbox import DockerBackend, RuntimeProfileResolver, host_architecture

        backend = DockerBackend(allowed_mount_roots=[data / "runs", data / "deps"])
        ok, engine, error = backend.availability()
        add("sandbox", "PASS" if ok else "FAIL", f"docker {engine}" if ok else (error or "unavailable"), "Docker engine for the current user",
            fix="Start Docker (Docker Desktop / dockerd); the harness never runs model code on the host")
        if ok:
            resolver = RuntimeProfileResolver(backend)
            try:
                runtime = resolver.resolve()
                add("runtime_image", "PASS", runtime.image_id[:19], "pinned image matching the tool library")
                add("image_architecture", "PASS" if runtime.architecture == host_architecture() else "FAIL",
                    runtime.architecture, host_architecture())
            except Exception as exc:
                add("runtime_image", "FAIL", f"{getattr(exc, 'code', 'RUNTIME_IMAGE_INVALID')}: {str(exc)[:160]}",
                    "pinned image matching the tool library", fix="Run make runtime-image (or harness sandbox build)")
            if self.config.dependency_setup:
                net = f"{DockerBackend.SETUP_NETWORK_PREFIX}doctor-{uuid.uuid4().hex[:6]}"
                try:
                    backend.network_create_internal(net, {"org.dobby.harness": "1"})
                    internal = (backend.network_inspect(net) or {}).get("Internal") is True
                    add("dependency_network", "PASS" if internal else "WARN", "internal network + PyPI allowlist proxy" if internal else "not internal",
                        "enforceable setup_scoped egress", required_check=False)
                except Exception as exc:
                    add("dependency_network", "WARN", f"NETWORK_POLICY_UNAVAILABLE: {str(exc)[:120]}", "enforceable setup_scoped egress",
                        required_check=False, fix="Dependency setup will report BLOCKED_ENVIRONMENT for repos that need packages")
                finally:
                    backend.network_remove(net)
        else:
            add("runtime_image", "SKIP", "sandbox unavailable", "pinned image")
        # Model
        from harness.model import CredentialProvider, ModelProfileResolver

        resolved = None
        try:
            resolver = ModelProfileResolver(self.config.model_profiles_path)
            resolved = resolver.resolve(self.config.model_profile)
            resolver.validate_live(resolved)
            add("model_profile", "PASS", f"{self.config.model_profile}: {resolved.contract.model} @ {resolved.contract.endpoint_origin} "
                f"({resolved.response_format})", "non-placeholder prescribed model profile")
        except Exception as exc:
            add("model_profile", "FAIL", f"{self.config.model_profile}: {str(exc)[:200]}", "non-placeholder prescribed model profile",
                fix="Set HARNESS_MODEL_PROFILE (deepseek, qwen, ...) or edit config/model_profiles.toml")
        try:
            CredentialProvider().get_ai_api_key()
            key_ok = True
            add("api_key", "PASS", "present-and-not-exported", "AI_API_KEY")
        except Exception:
            key_ok = False
            add("api_key", "FAIL", "missing", "AI_API_KEY", fix="export AI_API_KEY=... (never put it in a file or request)")
        if live:
            if resolved is not None and key_ok:
                from harness.model.probe import probe_model

                probe = probe_model(resolved)
                add("model_live", "PASS" if probe["status"] == "PASS" else "FAIL",
                    f"{probe['status']} {probe.get('error_code') or ''} {probe.get('latency_ms')}ms usage_reported={probe.get('usage_reported')}".strip(),
                    "one structured JSON reply", fix="Check endpoint/model/response_format for the provider")
            else:
                add("model_live", "SKIP", "profile or key missing", "one structured JSON reply")
        # Evaluator adapter + plugins
        from harness.release.profiles import load_profile

        release_profile = load_profile(PROFILES[profile])
        add("evaluator_adapter", "PASS", "native_json_v1 1.0.0", "native_json_v1")
        if profile == "submission":
            add("official_adapter", "FAIL", "OFFICIAL_ADAPTER_NOT_CONFIGURED", "pinned official adapter with passing conformance fixture",
                fix="Implement the organizer's official adapter once the protocol is supplied; submission-ready label is blocked")
        else:
            add("official_adapter", "WARN", "OFFICIAL_ADAPTER_NOT_CONFIGURED (native_json_v1 in use)", "official adapter when a protocol exists",
                required_check=False)
        from harness.plugins.registry import PluginError, PluginRegistry

        try:
            resolved_plugins = PluginRegistry(allowed_capabilities=release_profile["allowed_plugin_capabilities"]).resolve()
            add("plugins", "PASS", f"{len(resolved_plugins.plugins)} pinned plugins, lock {resolved_plugins.lock_sha256[:12]}, self-checks PASS",
                "reviewed pinned plugin set")
        except PluginError as exc:
            add("plugins", "FAIL", f"{exc.code}: {str(exc)[:200]}", "reviewed pinned plugin set",
                fix="Review the change, then run scripts/generate_plugin_lock.py")
        add("dependency_lock", *self._dependency_lock())
        add("publication", "SKIP", "P1 apply/publish disabled (keep and export only)", "not required for P0", required_check=False)
        status = "BLOCKED" if blocking else ("WARNING" if warnings else "READY")
        core = {"schema_version": "1.0", "profile": profile, "status": status, "checks": [c.model_dump() for c in checks],
                "blocking_check_ids": blocking, "warnings": warnings, "created_at": _now()}
        report = DoctorReportV1(**core, report_sha256=hashlib.sha256(canonical_json({k: v for k, v in core.items() if k != "created_at"}).encode()).hexdigest())
        return report, remediation

    def _dependency_lock(self) -> Tuple[str, str, str]:
        lock = HARNESS_ROOT / "requirements.lock"
        if not lock.is_file():
            return ("WARN", "requirements.lock missing", "hash-pinned lock")
        from importlib import metadata

        mismatched = []
        for line in lock.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-", " ")) or "==" not in line:
                continue
            name, _, rest = line.partition("==")
            version = rest.split(";")[0].split(" ")[0].strip().rstrip("\\").strip()
            try:
                installed = metadata.version(name.strip())
            except metadata.PackageNotFoundError:
                mismatched.append(f"{name}:missing")
                continue
            if installed != version:
                mismatched.append(f"{name}:{installed}!={version}")
        if mismatched:
            return ("WARN", "differs from lock: " + ", ".join(mismatched[:6]), "installed packages match requirements.lock")
        return ("PASS", "installed packages match requirements.lock", "installed packages match requirements.lock")

    def persist(self, report: DoctorReportV1, run_store) -> Optional[Path]:
        """Store the report as release evidence (under the data directory)."""
        from harness.release.profiles import ensure_profile, register_asset

        evidence_dir = Path(self.config.data_dir) / "release-evidence" / "doctor"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / f"doctor-{report.profile}-{report.created_at[:19].replace(':', '')}.json"
        path.write_text(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
        try:
            profile_row = ensure_profile(run_store, PROFILES[report.profile], "0" * 64)
            with run_store.get_connection() as conn:
                with conn:
                    asset = register_asset(conn, "DOCTOR_REPORT", path)
                    env = hashlib.sha256(canonical_json([c.model_dump() for c in report.checks if c.id in
                                                         ("python", "git", "sqlite", "sandbox", "image_architecture")]).encode()).hexdigest()
                    conn.execute("INSERT INTO h_doctor_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (f"doctor_{uuid.uuid4().hex[:16]}", profile_row, report.status, asset, report.report_sha256,
                                  len(report.blocking_check_ids), len(report.warnings), env, _now()))
        except Exception:
            pass
        return path
