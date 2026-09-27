"""Reviewed, pinned plugin resolution (PRD 6 sections 13.3-13.5).

Resolution happens once at process start, before any run, and fails closed:
lock hash, per-plugin content hash (checked against the exact module bytes
BEFORE import), interface major version, configuration schema, dependency DAG
(missing, conflicting, cyclic), capability subset of the release profile, review
status, and a bounded self-check. The lock is read only from the harness
installation (or a user-owned path outside the target repository); a target
repository can never add, select, or replace a plugin.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib
import importlib.util
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from harness.config import HARNESS_ROOT
from harness.contracts.release import PluginManifestV1, PluginSetLockV1
from harness.persistence import canonical_json
from harness.plugins.interfaces import INTERFACES, KERNEL_API_VERSION, PLUGIN_CAPABILITIES

PLUGIN_ROOT = HARNESS_ROOT / "config" / "plugins"
DEFAULT_LOCK = "builtin_release_v1.lock.json"


class PluginError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def lock_core(lock: Mapping[str, Any]) -> Dict[str, Any]:
    return {"schema_version": lock["schema_version"], "plugin_set_id": lock["plugin_set_id"],
            "kernel_api_version": lock["kernel_api_version"], "plugins": lock["plugins"]}


def module_source_path(module: str) -> Path:
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        raise PluginError("PLUGIN_MODULE_NOT_FOUND", f"Plugin module {module} is not an importable source module")
    return Path(spec.origin)


def module_content_sha256(module: str) -> str:
    return hashlib.sha256(module_source_path(module).read_bytes()).hexdigest()


def validate_config(schema: Mapping[str, Any], config: Any, path: str = "$") -> None:
    """Minimal JSON-Schema subset validator (type/enum/required/properties/additionalProperties/min/max)."""
    kind = schema.get("type")
    types = {"object": dict, "string": str, "integer": int, "boolean": bool, "array": list, "number": (int, float)}
    if kind and (not isinstance(config, types[kind]) or (kind in ("integer", "number") and isinstance(config, bool))):
        raise PluginError("PLUGIN_CONFIG_INVALID", f"{path}: expected {kind}")
    if "enum" in schema and config not in schema["enum"]:
        raise PluginError("PLUGIN_CONFIG_INVALID", f"{path}: value not in {schema['enum']}")
    if kind in ("integer", "number"):
        if "minimum" in schema and config < schema["minimum"]:
            raise PluginError("PLUGIN_CONFIG_INVALID", f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and config > schema["maximum"]:
            raise PluginError("PLUGIN_CONFIG_INVALID", f"{path}: above maximum {schema['maximum']}")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in config:
                raise PluginError("PLUGIN_CONFIG_INVALID", f"{path}.{key}: required")
        for key, value in config.items():
            if key not in properties:
                if schema.get("additionalProperties", True) is False:
                    raise PluginError("PLUGIN_CONFIG_INVALID", f"{path}.{key}: unknown property")
                continue
            validate_config(properties[key], value, f"{path}.{key}")


def _version_tuple(value: str) -> tuple:
    return tuple(int(part) for part in value.split("."))


def satisfies(version: str, constraint: str) -> bool:
    for clause in [c.strip() for c in constraint.split(",") if c.strip()]:
        for op in (">=", "<=", "==", ">", "<"):
            if clause.startswith(op):
                target = _version_tuple(clause[len(op):].strip())
                current = _version_tuple(version)
                if not {">=": current >= target, "<=": current <= target, "==": current == target,
                        ">": current > target, "<": current < target}[op]:
                    return False
                break
        else:
            raise PluginError("PLUGIN_DEPENDENCY_INVALID", f"Unsupported version constraint {constraint!r}")
    return True


def find_cycle(graph: Mapping[str, Sequence[str]]) -> Optional[List[str]]:
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        state[node] = 1
        stack.append(node)
        for neighbor in sorted(graph.get(node, [])):
            if state.get(neighbor) == 1:
                return stack[stack.index(neighbor):] + [neighbor]
            if neighbor not in state:
                found = visit(neighbor)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(graph):
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


@dataclass
class ResolvedPlugin:
    slot: str
    manifest: PluginManifestV1
    config: Dict[str, Any]
    configuration_sha256: str
    cls: Any
    self_check_status: str


@dataclass
class ResolvedPluginSet:
    lock: PluginSetLockV1
    plugins: Dict[str, ResolvedPlugin] = field(default_factory=dict)
    lock_path: Optional[Path] = None

    @property
    def lock_sha256(self) -> str:
        return self.lock.lock_sha256

    def cls(self, slot: str) -> Any:
        if slot not in self.plugins:
            raise PluginError("PLUGIN_SLOT_MISSING", f"No plugin bound to slot {slot!r}")
        return self.plugins[slot].cls

    def bindings(self) -> List[Dict[str, Any]]:
        return [{"slot": slot, "name": p.manifest.plugin.name, "version": p.manifest.plugin.version,
                 "content_sha256": p.manifest.plugin.content_sha256, "configuration_sha256": p.configuration_sha256,
                 "self_check_status": p.self_check_status} for slot, p in sorted(self.plugins.items())]


class PluginRegistry:
    def __init__(
        self,
        root: Path = PLUGIN_ROOT,
        lock_name: str = DEFAULT_LOCK,
        *,
        allowed_capabilities: Optional[Sequence[str]] = None,
        forbidden_roots: Sequence[Path] = (),
        self_checks: Optional[Mapping[str, Callable[[Any, Dict[str, Any]], None]]] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.lock_path = (self.root / lock_name).resolve()
        self.allowed_capabilities = set(allowed_capabilities if allowed_capabilities is not None else PLUGIN_CAPABILITIES)
        self.forbidden_roots = [Path(p).resolve() for p in forbidden_roots]
        if self_checks is None:
            from harness.plugins.self_checks import SELF_CHECKS

            self_checks = SELF_CHECKS
        self.self_checks = self_checks

    # ---------------------------------------------------------------- lock
    def load_lock(self) -> PluginSetLockV1:
        for root in self.forbidden_roots:
            if self.lock_path == root or root in self.lock_path.parents:
                raise PluginError("PLUGIN_LOCK_UNTRUSTED", "The plugin lock may not live inside the target repository")
        if not self.lock_path.is_file():
            raise PluginError("PLUGIN_LOCK_MISSING", f"Plugin set lock not found: {self.lock_path}")
        try:
            raw = json.loads(self.lock_path.read_text(encoding="utf-8"))
            lock = PluginSetLockV1.model_validate(raw)
        except (ValueError, TypeError) as exc:
            raise PluginError("PLUGIN_LOCK_INVALID", f"Plugin lock is invalid: {str(exc)[:300]}") from exc
        if _sha(lock_core(raw)) != lock.lock_sha256:
            raise PluginError("PLUGIN_LOCK_HASH_MISMATCH", "Plugin lock content does not match its lock_sha256")
        if lock.kernel_api_version.split(".")[0] != KERNEL_API_VERSION.split(".")[0]:
            raise PluginError("PLUGIN_API_INCOMPATIBLE", f"Lock targets kernel API {lock.kernel_api_version}")
        slots = [entry.slot for entry in lock.plugins]
        if len(slots) != len(set(slots)):
            raise PluginError("PLUGIN_LOCK_INVALID", "A slot is pinned more than once")
        return lock

    def _manifest(self, name: str, version: str) -> PluginManifestV1:
        path = self.root / "manifests" / f"{name}@{version}.json"
        if not path.is_file():
            raise PluginError("PLUGIN_MANIFEST_MISSING", f"No manifest for {name}@{version}")
        try:
            return PluginManifestV1.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise PluginError("PLUGIN_MANIFEST_INVALID", f"{name}@{version}: {str(exc)[:300]}") from exc

    # ------------------------------------------------------------- resolve
    def resolve(self) -> ResolvedPluginSet:
        lock = self.load_lock()
        manifests: Dict[str, PluginManifestV1] = {}
        configs: Dict[str, Dict[str, Any]] = {}
        for entry in lock.plugins:
            manifest = self._manifest(entry.name, entry.version)
            if manifest.plugin.content_sha256 != entry.content_sha256:
                raise PluginError("PLUGIN_HASH_MISMATCH", f"{entry.name}: manifest hash differs from the lock")
            actual = module_content_sha256(manifest.plugin.module)
            if actual != entry.content_sha256:
                raise PluginError("PLUGIN_HASH_MISMATCH",
                                  f"{entry.name}: module bytes changed since review (run scripts/generate_plugin_lock.py after review)")
            for interface in manifest.interfaces:
                if interface.name not in INTERFACES or interface.api_version.split(".")[0] != KERNEL_API_VERSION.split(".")[0]:
                    raise PluginError("PLUGIN_API_INCOMPATIBLE", f"{entry.name}: {interface.name} {interface.api_version}")
            if manifest.provenance.review_status != "REVIEWED":
                raise PluginError("PLUGIN_NOT_REVIEWED", f"{entry.name} review status is {manifest.provenance.review_status}")
            unknown = set(manifest.required_capabilities) - set(PLUGIN_CAPABILITIES)
            if unknown:
                raise PluginError("PLUGIN_CAPABILITY_UNKNOWN", f"{entry.name}: {sorted(unknown)}")
            excess = set(manifest.required_capabilities) - self.allowed_capabilities
            if excess:
                raise PluginError("PLUGIN_CAPABILITY_DENIED", f"{entry.name} requires capabilities the release profile denies: {sorted(excess)}")
            schema_path = self.root / "schemas" / f"{entry.name}@{entry.version}.schema.json"
            config_path = self.root / "config" / f"{entry.slot}.json"
            if not schema_path.is_file() or not config_path.is_file():
                raise PluginError("PLUGIN_CONFIG_MISSING", f"{entry.name}: configuration schema or configuration missing")
            schema_bytes = schema_path.read_bytes()
            if hashlib.sha256(schema_bytes).hexdigest() != manifest.configuration_schema_sha256:
                raise PluginError("PLUGIN_CONFIG_SCHEMA_MISMATCH", f"{entry.name}: configuration schema hash mismatch")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if _sha(config) != entry.configuration_sha256:
                raise PluginError("PLUGIN_CONFIG_HASH_MISMATCH", f"{entry.slot}: configuration differs from the lock")
            validate_config(json.loads(schema_bytes), config)
            manifests[entry.name] = manifest
            configs[entry.name] = config
        # Dependency DAG over plugin names.
        graph: Dict[str, List[str]] = {}
        for name, manifest in manifests.items():
            graph[name] = []
            for dependency in manifest.dependencies:
                target = manifests.get(dependency.name)
                if target is None:
                    if dependency.optional:
                        continue
                    raise PluginError("PLUGIN_DEPENDENCY_MISSING", f"{name} requires {dependency.name}")
                if not satisfies(target.plugin.version, dependency.version_constraint):
                    raise PluginError("PLUGIN_DEPENDENCY_CONFLICT",
                                      f"{name} requires {dependency.name} {dependency.version_constraint}, have {target.plugin.version}")
                graph[name].append(dependency.name)
        cycle = find_cycle(graph)
        if cycle:
            raise PluginError("PLUGIN_DEPENDENCY_CYCLE", "Plugin dependency cycle: " + " -> ".join(cycle))
        resolved = ResolvedPluginSet(lock=lock, lock_path=self.lock_path)
        by_name = {entry.name: entry for entry in lock.plugins}
        for name, manifest in manifests.items():
            module = importlib.import_module(manifest.plugin.module)
            cls = getattr(module, manifest.plugin.entry_point, None)
            if cls is None:
                raise PluginError("PLUGIN_ENTRY_POINT_MISSING", f"{name}: {manifest.plugin.entry_point} not found")
            check = self.self_checks.get(manifest.self_check)
            if check is None:
                raise PluginError("PLUGIN_SELF_CHECK_UNKNOWN", f"{name}: unknown self-check {manifest.self_check}")
            try:
                check(cls, configs[name])
            except PluginError:
                raise
            except Exception as exc:
                raise PluginError("PLUGIN_SELF_CHECK_FAILED", f"{name}: {manifest.self_check} failed: {str(exc)[:300]}") from exc
            entry = by_name[name]
            resolved.plugins[entry.slot] = ResolvedPlugin(entry.slot, manifest, configs[name], entry.configuration_sha256, cls, "PASS")
        return resolved

    # --------------------------------------------------------------- record
    def record(self, run_store, resolved: ResolvedPluginSet) -> Dict[str, str]:
        """Persist release assets, manifests, dependencies, the lock, and its entries (idempotent)."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entry_ids: Dict[str, str] = {}
        with run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                def asset(kind: str, path: Path, media: str = "application/json") -> str:
                    from harness.release.profiles import asset_path_key

                    relative = asset_path_key(path)
                    data = path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    row = conn.execute("SELECT release_asset_id, sha256 FROM h_release_assets WHERE relative_path = ?", (relative,)).fetchone()
                    if row and row["sha256"] == digest:
                        return row["release_asset_id"]
                    if row:
                        conn.execute("UPDATE h_release_assets SET sha256 = ?, byte_size = ?, created_at = ? WHERE release_asset_id = ?",
                                     (digest, len(data), now, row["release_asset_id"]))
                        return row["release_asset_id"]
                    asset_id = f"rasset_{uuid.uuid4().hex[:16]}"
                    conn.execute("INSERT INTO h_release_assets VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (asset_id, kind, relative, media, len(data), digest, now))
                    return asset_id

                lock_asset = asset("PLUGIN_SET_LOCK", self.lock_path)
                lock_row = conn.execute("SELECT plugin_set_lock_id FROM h_plugin_set_locks WHERE lock_sha256 = ?",
                                        (resolved.lock_sha256,)).fetchone()
                if lock_row:
                    lock_id = lock_row["plugin_set_lock_id"]
                else:
                    lock_id = f"plock_{uuid.uuid4().hex[:16]}"
                    version = resolved.lock_sha256[:12]
                    conn.execute("INSERT INTO h_plugin_set_locks VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?)",
                                 (lock_id, resolved.lock.plugin_set_id, version, resolved.lock.kernel_api_version,
                                  lock_asset, resolved.lock_sha256, now))
                for ordinal, (slot, plugin) in enumerate(sorted(resolved.plugins.items())):
                    identity = plugin.manifest.plugin
                    manifest_path = self.root / "manifests" / f"{identity.name}@{identity.version}.json"
                    schema_path = self.root / "schemas" / f"{identity.name}@{identity.version}.schema.json"
                    config_path = self.root / "config" / f"{slot}.json"
                    manifest_asset = asset("PLUGIN_MANIFEST", manifest_path)
                    schema_asset = asset("PLUGIN_CONFIG_SCHEMA", schema_path)
                    config_asset = asset("PLUGIN_CONFIGURATION", config_path)
                    row = conn.execute("SELECT plugin_manifest_id FROM h_plugin_manifests WHERE plugin_name = ? AND plugin_version = ? AND content_sha256 = ?",
                                       (identity.name, identity.version, identity.content_sha256)).fetchone()
                    if row:
                        manifest_id = row["plugin_manifest_id"]
                    else:
                        manifest_id = f"pman_{uuid.uuid4().hex[:16]}"
                        conn.execute(
                            "INSERT INTO h_plugin_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (manifest_id, identity.name, identity.version, identity.distribution, identity.module, identity.entry_point,
                             identity.content_sha256, canonical_json([i.model_dump() for i in plugin.manifest.interfaces]),
                             schema_asset, plugin.manifest.configuration_schema_sha256,
                             canonical_json({"required": plugin.manifest.required_capabilities, "optional": plugin.manifest.optional_capabilities}),
                             manifest_asset, plugin.manifest.provenance.license, plugin.manifest.provenance.review_status,
                             1 if plugin.manifest.in_process else 0, manifest_asset,
                             hashlib.sha256(manifest_path.read_bytes()).hexdigest(), now),
                        )
                        for dependency in plugin.manifest.dependencies:
                            conn.execute("INSERT OR IGNORE INTO h_plugin_dependencies VALUES (?, ?, ?, ?, ?, NULL, ?)",
                                         (f"pdep_{uuid.uuid4().hex[:16]}", manifest_id, dependency.name, dependency.version_constraint,
                                          1 if dependency.optional else 0, now))
                    existing = conn.execute("SELECT plugin_set_entry_id FROM h_plugin_set_entries WHERE plugin_set_lock_id = ? AND slot = ?",
                                            (lock_id, slot)).fetchone()
                    if existing:
                        entry_ids[slot] = existing["plugin_set_entry_id"]
                    else:
                        entry_id = f"pent_{uuid.uuid4().hex[:16]}"
                        conn.execute("INSERT INTO h_plugin_set_entries VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     (entry_id, lock_id, slot, manifest_id, config_asset, plugin.configuration_sha256, ordinal))
                        entry_ids[slot] = entry_id
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return entry_ids

    def bind_run(self, run_store, run_id: str, resolved: ResolvedPluginSet) -> None:
        """Freeze the resolved plugin set for ``run_id`` (UNIQUE(run, slot); a rerun must match)."""
        from harness.persistence.events import append_event_sql

        entry_ids = self.record(run_store, resolved)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for slot, plugin in sorted(resolved.plugins.items()):
                    identity = plugin.manifest.plugin
                    row = conn.execute("SELECT content_sha256, configuration_sha256 FROM h_run_plugin_bindings WHERE run_id = ? AND slot = ?",
                                       (run_id, slot)).fetchone()
                    if row:
                        if row["content_sha256"] != identity.content_sha256 or row["configuration_sha256"] != plugin.configuration_sha256:
                            raise PluginError("PLUGIN_BINDING_CHANGED",
                                              f"Run {run_id} is frozen to a different {slot} plugin; start a new run to change plugins")
                        continue
                    conn.execute("INSERT INTO h_run_plugin_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (f"pbind_{uuid.uuid4().hex[:16]}", run_id, entry_ids[slot], slot, identity.name, identity.version,
                                  identity.content_sha256, plugin.configuration_sha256, plugin.self_check_status, now))
                    append_event_sql(conn, run_id, "PLUGIN_BOUND", {"slot": slot, "name": identity.name, "version": identity.version,
                                                                   "content_sha256": identity.content_sha256}, "PRD6", "PRD6", now)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
