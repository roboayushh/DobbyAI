#!/usr/bin/env python3
"""Regenerate config/plugins (manifests, configuration schemas, configurations, lock)
from the reviewed built-in catalog and the exact bytes of each plugin module.

Run after reviewing a change to any plugin module; the harness refuses to start
with a stale lock (content-hash mismatch). `--check` exits 1 when the lock is stale.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.persistence import canonical_json  # noqa: E402
from harness.plugins.catalog import BUILTIN_SET_ID, CATALOG  # noqa: E402
from harness.plugins.interfaces import KERNEL_API_VERSION  # noqa: E402
from harness.plugins.registry import PLUGIN_ROOT, lock_core, module_content_sha256  # noqa: E402


def sha(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def pretty(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def build() -> dict:
    files = {}
    entries = []
    for item in CATALOG:
        name, version = item["name"], item["version"]
        schema_text = pretty(item["config_schema"])
        files[f"schemas/{name}@{version}.schema.json"] = schema_text
        files[f"config/{item['slot']}.json"] = pretty(item["config"])
        content = module_content_sha256(item["module"])
        manifest = {
            "schema_version": "1.0",
            "plugin": {"name": name, "version": version, "distribution": "ai-coding-harness", "module": item["module"],
                       "entry_point": item["entry_point"], "content_sha256": content},
            "interfaces": [{"name": item["interface"], "api_version": KERNEL_API_VERSION}],
            "configuration_schema_sha256": hashlib.sha256(schema_text.encode()).hexdigest(),
            "required_capabilities": item["capabilities"],
            "optional_capabilities": [],
            "dependencies": [{"name": d["name"], "version_constraint": d["version_constraint"], "optional": False} for d in item["dependencies"]],
            "runtime": {"python": ">=3.12,<3.14", "platforms": ["linux", "darwin"]},
            "provenance": {"source": "project-built-in", "license": "project-license", "review_status": "REVIEWED",
                           "review_artifact_id": None},
            "in_process": True,
            "self_check": item["self_check"],
        }
        files[f"manifests/{name}@{version}.json"] = pretty(manifest)
        entries.append({"slot": item["slot"], "name": name, "version": version, "content_sha256": content,
                        "configuration_sha256": sha(item["config"])})
    lock = {"schema_version": "1.0", "plugin_set_id": BUILTIN_SET_ID, "kernel_api_version": KERNEL_API_VERSION,
            "plugins": sorted(entries, key=lambda e: e["slot"])}
    lock["lock_sha256"] = sha(lock_core(lock))
    lock["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return files, lock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files, lock = build()
    lock_path = PLUGIN_ROOT / "builtin_release_v1.lock.json"
    if args.check:
        current = json.loads(lock_path.read_text()) if lock_path.is_file() else {}
        stale = current.get("lock_sha256") != lock["lock_sha256"] or any(
            not (PLUGIN_ROOT / rel).is_file() or (PLUGIN_ROOT / rel).read_text() != text for rel, text in files.items())
        print("plugin lock is STALE; run scripts/generate_plugin_lock.py" if stale else "plugin lock is current")
        return 1 if stale else 0
    for rel, text in files.items():
        path = PLUGIN_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    current = json.loads(lock_path.read_text()) if lock_path.is_file() else {}
    if current.get("lock_sha256") != lock["lock_sha256"]:
        lock_path.write_text(pretty(lock))
    print(f"plugin lock {lock['lock_sha256'][:16]} ({len(lock['plugins'])} plugins)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
