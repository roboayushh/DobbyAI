"""Pure, deterministic queue planning functions (PRD 5 section 8).

No I/O happens here: classification, DAG validation, stable cycle reporting,
readiness, blocked reasons, criticality, and scheduler ordering are all
functions of their inputs so identical frozen inputs yield identical plans.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

DEPENDENCY_PATTERN = re.compile(
    r"(?i)\b(?:depends\s+on|blocked\s+by|requires|after|needs)\s*:?\s+((?:#\d+[\s,and&]*)+)"
)
ISSUE_NUMBER = re.compile(r"#(\d+)")
UNSUPPORTED_LABELS = {"question", "discussion", "wontfix", "invalid", "duplicate"}
PRIORITY_LABELS = {"priority:critical": 400, "priority:high": 300, "priority:medium": 200, "priority:low": 100}
INTEGRATED = "INTEGRATED"


class QueuePlanError(ValueError):
    code = "QUEUE_PLAN_INVALID"


@dataclass(frozen=True)
class TaskFacts:
    task_id: str
    ordinal: int
    title: str
    body: str
    labels: Tuple[str, ...]
    source_key: str
    normalized_sha256: str
    created_at: Optional[str] = None

    @property
    def issue_number(self) -> Optional[int]:
        match = re.search(r":issue:(\d+)$", self.source_key)
        return int(match.group(1)) if match else None


@dataclass
class ClassifiedItem:
    task_id: str
    ordinal: int
    classification: str
    priority: int
    canonical_task_id: Optional[str] = None
    reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Edge:
    predecessor: str  # task_id
    dependent: str  # task_id
    source: str = "SOURCE_METADATA"


def priority_of(labels: Iterable[str]) -> int:
    return max((PRIORITY_LABELS.get(label.lower(), 0) for label in labels), default=0)


def classify(tasks: Sequence[TaskFacts]) -> Tuple[List[ClassifiedItem], List[Edge]]:
    """Deterministic host classification of frozen tasks."""
    ordered = sorted(tasks, key=lambda item: (item.ordinal, item.task_id))
    by_number = {task.issue_number: task.task_id for task in ordered if task.issue_number is not None}
    seen_hash: Dict[str, str] = {}
    items: List[ClassifiedItem] = []
    edges: List[Edge] = []
    for task in ordered:
        item = ClassifiedItem(task.task_id, task.ordinal, "ACTIONABLE", priority_of(task.labels))
        labels = {label.lower() for label in task.labels}
        text = f"{task.title}\n{task.body}".strip()
        if task.normalized_sha256 in seen_hash and task.body.strip():
            item.classification = "DUPLICATE"
            item.canonical_task_id = seen_hash[task.normalized_sha256]
            item.reasons.append("DUPLICATE_OF_SELECTED_TASK")
        elif labels & UNSUPPORTED_LABELS:
            item.classification = "UNSUPPORTED"
            item.reasons.append("UNSUPPORTED_LABEL:" + ",".join(sorted(labels & UNSUPPORTED_LABELS)))
        elif len(text.split()) < 3:
            item.classification = "AMBIGUOUS"
            item.reasons.append("INSUFFICIENT_TASK_DESCRIPTION")
        else:
            predecessors = []
            for match in DEPENDENCY_PATTERN.finditer(text):
                for number in ISSUE_NUMBER.findall(match.group(1)):
                    target = by_number.get(int(number))
                    if target and target != task.task_id and target not in predecessors:
                        predecessors.append(target)
            if predecessors:
                item.classification = "DEPENDENT"
                item.reasons.extend(f"REQUIRES:{target}" for target in predecessors)
                edges.extend(Edge(target, task.task_id) for target in predecessors)
        if task.body.strip():
            seen_hash.setdefault(task.normalized_sha256, task.task_id)
        items.append(item)
    return items, edges


def validate(items: Sequence[ClassifiedItem], edges: Sequence[Edge], *, max_tasks: int, max_edges: int) -> None:
    ids = [item.task_id for item in items]
    if len(ids) != len(set(ids)):
        raise QueuePlanError("Duplicate task IDs in queue plan")
    if len(items) > max_tasks:
        raise QueuePlanError(f"Queue has {len(items)} items but the policy maximum is {max_tasks}")
    if len(edges) > max_edges:
        raise QueuePlanError(f"Queue has {len(edges)} edges but the policy maximum is {max_edges}")
    known = set(ids)
    by_id = {item.task_id: item for item in items}
    for item in items:
        if item.classification == "DUPLICATE":
            if not item.canonical_task_id or item.canonical_task_id == item.task_id:
                raise QueuePlanError(f"Duplicate {item.task_id} has no valid canonical item")
            canonical = by_id.get(item.canonical_task_id)
            if canonical is None:
                raise QueuePlanError(f"Duplicate {item.task_id} points to a missing item")
            if canonical.classification == "DUPLICATE":
                raise QueuePlanError(f"Duplicate chain detected at {item.task_id}")
        if item.classification in ("AMBIGUOUS", "UNSUPPORTED", "INVALID") and not item.reasons:
            raise QueuePlanError(f"{item.classification} item {item.task_id} lacks a reason code")
    for edge in edges:
        if edge.predecessor not in known or edge.dependent not in known:
            raise QueuePlanError(f"Edge references unknown task: {edge.predecessor} -> {edge.dependent}")
        if edge.predecessor == edge.dependent:
            raise QueuePlanError(f"Self dependency on {edge.dependent}")
    cycle = find_cycle(ids, edges)
    if cycle:
        raise QueuePlanError("Dependency cycle: " + " -> ".join(cycle))


def find_cycle(nodes: Sequence[str], edges: Sequence[Edge]) -> Optional[List[str]]:
    """Smallest-first stable cycle path, or None for a DAG."""
    adjacency: Dict[str, List[str]] = {node: [] for node in nodes}
    for edge in edges:
        adjacency.setdefault(edge.predecessor, []).append(edge.dependent)
    for node in adjacency:
        adjacency[node] = sorted(set(adjacency[node]))
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        state[node] = 1
        stack.append(node)
        for neighbor in adjacency.get(node, []):
            if state.get(neighbor) == 1:
                return stack[stack.index(neighbor):] + [neighbor]
            if neighbor not in state:
                found = visit(neighbor)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(adjacency):
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def predecessors_of(task_id: str, edges: Sequence[Edge]) -> List[str]:
    return sorted({edge.predecessor for edge in edges if edge.dependent == task_id})


def criticality(edges: Sequence[Edge]) -> Dict[str, int]:
    """Number of transitive dependents for each node."""
    children: Dict[str, Set[str]] = {}
    for edge in edges:
        children.setdefault(edge.predecessor, set()).add(edge.dependent)
    memo: Dict[str, Set[str]] = {}

    def reach(node: str) -> Set[str]:
        if node in memo:
            return memo[node]
        result: Set[str] = set()
        for child in children.get(node, set()):
            result.add(child)
            result |= reach(child)
        memo[node] = result
        return result

    return {node: len(reach(node)) for node in children}


def ready_items(
    items: Sequence[Mapping[str, object]],
    edges: Sequence[Edge],
    states: Mapping[str, str],
) -> List[str]:
    """Task IDs that are actionable, pending, and whose predecessors all integrated."""
    ready: List[str] = []
    for item in items:
        task_id = str(item["task_id"])
        if item["classification"] not in ("ACTIONABLE", "DEPENDENT"):
            continue
        if states.get(task_id) not in ("PENDING", "READY"):
            continue
        if all(states.get(pred) == INTEGRATED for pred in predecessors_of(task_id, edges)):
            ready.append(task_id)
    return ready


def blocked_reason(task_id: str, edges: Sequence[Edge], states: Mapping[str, str]) -> Tuple[Optional[str], List[str]]:
    blocking = [pred for pred in predecessors_of(task_id, edges) if states.get(pred) != INTEGRATED]
    if not blocking:
        return None, []
    return "DEPENDENCY_NOT_INTEGRATED", blocking


def order(
    candidates: Sequence[str],
    items: Mapping[str, Mapping[str, object]],
    edges: Sequence[Edge],
) -> List[str]:
    """Stable scheduler order: priority, criticality, source time, ordinal, task ID."""
    crit = criticality(edges)

    def key(task_id: str):
        item = items[task_id]
        created = item.get("created_at") or "9999"
        return (-int(item.get("priority", 0)), -crit.get(task_id, 0), str(created), int(item["ordinal"]), task_id)

    return sorted(candidates, key=key)
