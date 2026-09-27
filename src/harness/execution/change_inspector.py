"""Deterministic classification of host-observed workspace changes.

Same inputs always yield the same classification (NFR3-005). Helper-tool
reports and stdout never participate; only the two host manifests do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from harness.policy.engine import path_within
from harness.workspace.manifest import ChangeSet, FileChange, is_disposable


@dataclass(frozen=True)
class ClassifiedChange:
    change: FileChange
    declared: bool
    policy_state: str  # ALLOWED, UNEXPECTED, DENIED, DISPOSABLE
    reason: Optional[str] = None


@dataclass
class ChangeClassification:
    items: List[ClassifiedChange] = field(default_factory=list)
    special: List[str] = field(default_factory=list)
    reserved: List[str] = field(default_factory=list)
    unsafe_names: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    growth_bytes: int = 0
    new_files: int = 0

    @property
    def allowed(self) -> List[ClassifiedChange]:
        return [item for item in self.items if item.policy_state == "ALLOWED"]

    @property
    def disposable(self) -> List[ClassifiedChange]:
        return [item for item in self.items if item.policy_state == "DISPOSABLE"]

    @property
    def violations(self) -> List[ClassifiedChange]:
        return [item for item in self.items if item.policy_state in ("UNEXPECTED", "DENIED")]

    @property
    def is_violation(self) -> bool:
        return bool(self.violations or self.special or self.reserved or self.unsafe_names or self.reasons)

    def unexpected_paths(self) -> List[str]:
        paths = [item.change.path for item in self.violations]
        paths.extend(self.special)
        paths.extend(self.reserved)
        paths.extend(self.unsafe_names)
        return sorted(set(paths))


class ChangeInspector:
    def classify(
        self,
        change_set: ChangeSet,
        declared_paths: Sequence[str],
        *,
        max_growth_bytes: int,
        max_new_files: int,
    ) -> ChangeClassification:
        result = ChangeClassification()
        result.special = [path for path, _ in change_set.special]
        result.reserved = list(change_set.reserved)
        result.unsafe_names = list(change_set.unsafe_names)
        if result.special:
            result.reasons.append("SPECIAL_FILE_CREATED")
        if result.reserved:
            result.reasons.append("RESERVED_PATH_WRITTEN")
        if result.unsafe_names:
            result.reasons.append("UNSAFE_FILE_NAME")
        for change in change_set.changes:
            touched = [change.path] + ([change.old_path] if change.old_path else [])
            if all(is_disposable(path) for path in touched):
                result.items.append(ClassifiedChange(change, False, "DISPOSABLE"))
                continue
            declared = all(path_within(path, declared_paths) for path in touched)
            if change.change_type == "SYMLINK_CHANGED":
                result.items.append(ClassifiedChange(change, declared, "DENIED", "SYMLINK_WRITE"))
                continue
            if not declared:
                result.items.append(ClassifiedChange(change, False, "UNEXPECTED", "UNDECLARED_PATH"))
                continue
            result.items.append(ClassifiedChange(change, True, "ALLOWED"))
        counted = [item for item in result.items if item.policy_state != "DISPOSABLE"]
        result.growth_bytes = sum(item.change.byte_delta for item in counted)
        result.new_files = sum(1 for item in counted if item.change.change_type == "CREATED")
        if result.growth_bytes > max_growth_bytes:
            result.reasons.append("WORKSPACE_GROWTH_LIMIT")
        if result.new_files > max_new_files:
            result.reasons.append("NEW_FILE_LIMIT")
        if any(item.policy_state == "UNEXPECTED" for item in result.items):
            result.reasons.append("UNDECLARED_PATH")
        if any(item.policy_state == "DENIED" for item in result.items):
            result.reasons.append("DENIED_CHANGE")
        result.reasons = sorted(set(result.reasons))
        return result
