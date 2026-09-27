"""Pinned release profiles and retention policies (PRD 6 sections 4 and 12)."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from harness.config import HARNESS_ROOT

PROFILE_DIR = HARNESS_ROOT / "config" / "profiles"
RETENTION_DIR = HARNESS_ROOT / "config" / "retention"
DEFAULT_PROFILE = "evaluation_strict_v1"


class ReleaseProfileError(ValueError):
    code = "INVALID_RELEASE_PROFILE"


def load_profile(name: str) -> Dict[str, Any]:
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ReleaseProfileError(f"Invalid release profile name: {name!r}")
    path = PROFILE_DIR / f"{name}.json"
    if not path.is_file():
        raise ReleaseProfileError(f"Unknown release profile {name!r}; available: {', '.join(p.stem for p in sorted(PROFILE_DIR.glob('*.json')))}")
    return json.loads(path.read_text(encoding="utf-8"))


def asset_path_key(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(HARNESS_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def register_asset(conn, kind: str, path: Path, now: Optional[str] = None) -> str:
    return _asset(conn, kind, Path(path), now or datetime.datetime.now(datetime.timezone.utc).isoformat())


def _asset(conn, kind: str, path: Path, now: str) -> str:
    relative = asset_path_key(path)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT release_asset_id, sha256 FROM h_release_assets WHERE relative_path = ?", (relative,)).fetchone()
    if row:
        if row["sha256"] != digest:
            conn.execute("UPDATE h_release_assets SET sha256 = ?, byte_size = ?, created_at = ? WHERE release_asset_id = ?",
                         (digest, len(data), now, row["release_asset_id"]))
        return row["release_asset_id"]
    asset_id = f"rasset_{uuid.uuid4().hex[:16]}"
    conn.execute("INSERT INTO h_release_assets VALUES (?, ?, ?, ?, ?, ?, ?)", (asset_id, kind, relative, "application/json", len(data), digest, now))
    return asset_id


def ensure_profile(run_store, name: str, plugin_lock_sha256: str) -> str:
    """Register (idempotently) the release profile and its retention policy; return the profile row id."""
    profile = load_profile(name)
    path = PROFILE_DIR / f"{name}.json"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with run_store.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            asset_id = _asset(conn, "RELEASE_PROFILE", path, now)
            row = conn.execute("SELECT release_profile_id, configuration_sha256, plugin_set_lock_sha256 FROM h_release_profiles WHERE name = ? AND version = ?",
                               (profile["name"], profile["version"])).fetchone()
            if row:
                profile_id = row["release_profile_id"]
                if row["configuration_sha256"] != digest or row["plugin_set_lock_sha256"] != plugin_lock_sha256:
                    conn.execute("UPDATE h_release_profiles SET configuration_sha256 = ?, plugin_set_lock_sha256 = ? WHERE release_profile_id = ?",
                                 (digest, plugin_lock_sha256, profile_id))
            else:
                profile_id = f"rprof_{uuid.uuid4().hex[:16]}"
                conn.execute("INSERT INTO h_release_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)",
                             (profile_id, profile["name"], profile["version"], profile["release_level"], asset_id, digest,
                              profile["evaluator_adapter"]["name"], profile["evaluator_adapter"]["version"], plugin_lock_sha256, now))
            retention_name = profile.get("retention_policy", "retain_default_v1")
            retention_path = RETENTION_DIR / "default_v1.json"
            retention = json.loads(retention_path.read_text(encoding="utf-8"))
            if not conn.execute("SELECT 1 FROM h_retention_policies WHERE name = ? AND version = ?",
                                (retention["name"], retention["version"])).fetchone():
                retention_asset = _asset(conn, "RETENTION_POLICY", retention_path, now)
                conn.execute("INSERT INTO h_retention_policies VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)",
                             (f"retain_{uuid.uuid4().hex[:12]}", retention["name"], retention["version"], retention_asset,
                              hashlib.sha256(retention_path.read_bytes()).hexdigest(), now))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return profile_id


def retention_policy(run_store) -> Dict[str, Any]:
    policy = json.loads((RETENTION_DIR / "default_v1.json").read_text(encoding="utf-8"))
    with run_store.get_connection() as conn:
        row = conn.execute("SELECT retention_policy_id FROM h_retention_policies WHERE name = ? AND version = ?",
                           (policy["name"], policy["version"])).fetchone()
    policy["retention_policy_id"] = row["retention_policy_id"] if row else None
    return policy
