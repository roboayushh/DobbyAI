"""Deterministic bounded lexical, symbol, and adjacent-test retrieval."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from harness.contracts import EvidenceQueryV1, EvidenceRefV1, TruthStatus
from harness.persistence import RunStore, canonical_json
from harness.retrieval.evidence_store import EvidenceStore
from harness.retrieval.indexer import INSTRUCTION_NAMES, QUERY_VERSION, RepositoryIndexer


@dataclass(frozen=True)
class EvidenceResult:
    reference: EvidenceRefV1
    content: str
    score: int
    score_components: Tuple[str, ...]


class Retriever:
    def __init__(
        self,
        run_store: RunStore,
        indexer: RepositoryIndexer,
        evidence_store: EvidenceStore,
        *,
        max_line_span: int = 80,
    ) -> None:
        self.run_store = run_store
        self.indexer = indexer
        self.evidence_store = evidence_store
        self.max_line_span = max_line_span

    def query(
        self,
        run_id: str,
        task_id: str,
        source_revision: str,
        query: EvidenceQueryV1,
    ) -> List[EvidenceResult]:
        root = self.indexer.workspace_root(run_id)
        candidates = self._candidate_rows(run_id, source_revision, query)
        results: List[Tuple[int, str, int, Dict[str, Any], str, str, Tuple[str, ...], int]] = []
        for row, start_line, end_line, reason, components in candidates:
            path = (root / row["relative_path"]).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["content_sha256"]:
                continue
            lines = raw.decode("utf-8").splitlines()
            start = max(1, start_line)
            end = min(len(lines), max(start, end_line), start + self.max_line_span - 1)
            span = lines[start - 1 : end]
            content = "\n".join(span)
            # The stored end line is the exact last line read. Deriving it from
            # content.splitlines() drops trailing blank lines and breaks span re-verification.
            span_end = start + max(0, len(span) - 1)
            score = self._score(components)
            results.append((score, row["relative_path"], start, row, content, reason, components, span_end))
        results.sort(key=lambda item: (-item[0], item[1], item[2]))

        admitted: List[EvidenceResult] = []
        total_bytes = 0
        for score, path, start, row, content, reason, components, span_end in results:
            if len(admitted) >= query.max_results:
                break
            size = len(content.encode("utf-8"))
            if total_bytes + size > query.max_bytes:
                continue
            total_bytes += size
            end = span_end
            stable = canonical_json(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "revision": source_revision,
                    "query": query.model_dump(mode="json"),
                    "path": path,
                    "start": start,
                    "end": end,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
            evidence_id = f"evd_{hashlib.sha256(stable.encode()).hexdigest()[:20]}"
            evidence_type = (
                "untrusted_repository_instruction"
                if Path(path).name.lower() in INSTRUCTION_NAMES
                else self._evidence_type(query.query_type)
            )
            reference = self.evidence_store.put(
                evidence_id=evidence_id,
                run_id=run_id,
                task_id=task_id,
                source_revision=source_revision,
                relative_path=path,
                start_line=start,
                end_line=end,
                symbol=row.get("qualified_name"),
                evidence_type=evidence_type,
                retrieval_reason=reason,
                truth_status=TruthStatus.OBSERVED,
                content=content,
                parser_version=row.get("parser_version"),
                provenance={
                    "query_version": QUERY_VERSION,
                    "query_type": query.query_type,
                    "query": query.query,
                    "file_sha256": row["content_sha256"],
                    "score": score,
                    "score_components": list(components),
                },
            )
            admitted.append(EvidenceResult(reference, content, score, components))
        return admitted

    def get(self, evidence_id: str) -> Dict[str, Any]:
        return self.evidence_store.get(evidence_id)

    def invalidate(
        self, changed_paths: List[str], old_workspace_version: str, new_workspace_version: str
    ) -> int:
        return self.evidence_store.invalidate(
            changed_paths, old_workspace_version, new_workspace_version
        )

    def _candidate_rows(
        self, run_id: str, source_revision: str, query: EvidenceQueryV1
    ) -> List[Tuple[Dict[str, Any], int, int, str, Tuple[str, ...]]]:
        with self.run_store.get_connection() as conn:
            files = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM h_file_index
                    WHERE run_id = ? AND source_revision = ? AND valid = 1
                      AND parse_state <> 'EXCLUDED'
                    ORDER BY relative_path
                    """,
                    (run_id, source_revision),
                ).fetchall()
            ]
            symbols = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT s.*, f.relative_path, f.content_sha256 AS file_sha256,
                           f.parser_version, f.byte_size, f.language, f.parse_state,
                           f.exclusion_reason, f.valid, f.run_id, f.source_revision
                    FROM h_symbols s JOIN h_file_index f ON f.file_index_id = s.file_index_id
                    WHERE f.run_id = ? AND f.source_revision = ? AND f.valid = 1
                    ORDER BY f.relative_path, s.start_line, s.qualified_name
                    """,
                    (run_id, source_revision),
                ).fetchall()
            ]
        file_by_path = {row["relative_path"]: row for row in files}
        root = self.indexer.workspace_root(run_id)
        kind = query.query_type
        needle = query.query.strip()
        candidates: List[Tuple[Dict[str, Any], int, int, str, Tuple[str, ...]]] = []
        # A text/identifier query that exactly names an indexed file (a common model mistake)
        # returns that file instead of an empty result.
        if kind in ("EXACT_TEXT", "IDENTIFIER", "SYMBOL_DEFINITION") and needle.removeprefix("./") in file_by_path:
            row = file_by_path[needle.removeprefix("./")]
            return [(row, 1, min(self.max_line_span, self._line_count(root, row)), "query named an indexed file", ("exact_path",))]
        if kind == "PATH_GLOB":
            patterns = _glob_variants(needle.removeprefix("./"))
            for row in files:
                if any(fnmatch.fnmatchcase(row["relative_path"], pattern) for pattern in patterns):
                    exact = row["relative_path"] == needle
                    span = self.max_line_span if exact else 40
                    candidates.append((row, 1, min(span, self._line_count(root, row)), "path glob match", ("exact_path",)))
            return candidates
        if kind in {"SYMBOL_DEFINITION", "SYMBOL_REFERENCES"}:
            expected_kinds = {"reference"} if kind == "SYMBOL_REFERENCES" else {
                "function", "class", "method", "interface", "type", "enum"
            }
            for symbol in symbols:
                leaf = symbol["qualified_name"].split(".")[-1]
                if symbol["symbol_kind"] in expected_kinds and leaf == needle:
                    row = dict(file_by_path[symbol["relative_path"]])
                    row.update(
                        qualified_name=symbol["qualified_name"],
                        parser_version=symbol["parser_version"],
                    )
                    candidates.append(
                        (
                            row,
                            symbol["start_line"],
                            min(symbol["end_line"], symbol["start_line"] + self.max_line_span - 1),
                            f"exact {kind.lower()} match for {needle}",
                            ("exact_symbol", "definition" if kind == "SYMBOL_DEFINITION" else "reference"),
                        )
                    )
            return candidates
        if kind == "STACK_TRACE_LOCATION":
            match = re.search(r"(?P<path>[A-Za-z0-9_./\\-]+):(?P<line>\d+)", needle)
            if match:
                wanted = match.group("path").replace("\\", "/")
                line = int(match.group("line"))
                matched_paths: List[str] = []
                for row in files:
                    if row["relative_path"] == wanted or row["relative_path"].endswith(wanted):
                        matched_paths.append(row["relative_path"])
                        candidates.append(
                            (row, max(1, line - 8), line + 12, "exact stack trace path and line", ("stack_trace", "exact_path"))
                        )
                symbol_names = {
                    symbol["qualified_name"].split(".")[-1]
                    for symbol in symbols
                    if symbol["relative_path"] in matched_paths
                    and symbol["start_line"] <= line <= symbol["end_line"]
                }
                for row in files:
                    if not self._is_test_path(row["relative_path"]):
                        continue
                    text = (root / row["relative_path"]).read_text(encoding="utf-8")
                    for line_no, text_line in enumerate(text.splitlines(), start=1):
                        if any(
                            re.search(rf"\b{re.escape(symbol_name)}\b", text_line)
                            for symbol_name in symbol_names
                        ):
                            candidates.append(
                                (
                                    row,
                                    max(1, line_no - 6),
                                    line_no + 10,
                                    "adjacent test referencing stack-trace symbol",
                                    ("adjacent_test", "lexical"),
                                )
                            )
                            break
            return candidates
        if kind == "MANIFEST_OR_CONFIG":
            config_names = {
                "pyproject.toml", "package.json", "tsconfig.json", "setup.cfg",
                "tox.ini", "Makefile", "Dockerfile", "requirements.txt",
            }
            for row in files:
                if Path(row["relative_path"]).name in config_names:
                    candidates.append((row, 1, 60, "runtime manifest or configuration", ("manifest",)))
            return candidates
        if kind == "INSTRUCTION_FILE":
            for row in files:
                if Path(row["relative_path"]).name.lower() in INSTRUCTION_NAMES:
                    candidates.append((row, 1, 80, "repository instruction file (untrusted)", ("instruction",)))
            return candidates

        escaped = re.escape(needle)
        if kind in {"IDENTIFIER", "ADJACENT_TESTS", "IMPORT_NEIGHBORS"}:
            pattern = re.compile(rf"\b{escaped}\b", re.IGNORECASE)
        else:
            pattern = re.compile(escaped, re.IGNORECASE)
        for row in files:
            if kind == "ADJACENT_TESTS" and not self._is_test_path(row["relative_path"]):
                continue
            path = root / row["relative_path"]
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    components: Tuple[str, ...]
                    if kind == "ADJACENT_TESTS":
                        components = ("adjacent_test", "lexical")
                    elif kind == "IMPORT_NEIGHBORS":
                        components = ("import_neighbor", "lexical")
                    elif kind == "IDENTIFIER":
                        components = ("exact_identifier",)
                    else:
                        components = ("exact_text",)
                    candidates.append(
                        (
                            row,
                            max(1, line_no - 6),
                            line_no + 10,
                            f"{kind.lower()} match on line {line_no}",
                            components,
                        )
                    )
                    break
        return candidates

    @staticmethod
    def _score(components: Sequence[str]) -> int:
        weights = {
            "stack_trace": 1000,
            "exact_path": 900,
            "exact_symbol": 800,
            "definition": 100,
            "exact_identifier": 700,
            "adjacent_test": 600,
            "reference": 500,
            "import_neighbor": 400,
            "manifest": 300,
            "exact_text": 200,
            "instruction": 100,
            "lexical": 10,
        }
        return sum(weights.get(component, 0) for component in components)

    @staticmethod
    def _line_count(root: Path, row: Dict[str, Any]) -> int:
        return len((root / row["relative_path"]).read_text(encoding="utf-8").splitlines())

    @staticmethod
    def _is_test_path(path: str) -> bool:
        name = Path(path).name.lower()
        return (
            path.startswith(("tests/", "test/", "spec/", "__tests__/"))
            or name.startswith("test_")
            or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        )

    @staticmethod
    def _evidence_type(query_type: str) -> str:
        return {
            "SYMBOL_DEFINITION": "definition",
            "SYMBOL_REFERENCES": "reference",
            "ADJACENT_TESTS": "adjacent_test",
            "STACK_TRACE_LOCATION": "stack_trace",
            "MANIFEST_OR_CONFIG": "manifest",
        }.get(query_type, "source_excerpt")


def _glob_variants(pattern: str, limit: int = 64) -> List[str]:
    """Shell-style globs as models write them: ``{a,b}`` alternatives and ``**/`` for zero or
    more directories (``fnmatch`` supports neither)."""
    variants = [pattern]
    expanded: List[str] = []
    while variants and len(expanded) + len(variants) <= limit:
        current = variants.pop()
        match = re.search(r"\{([^{}]*)\}", current)
        if not match:
            expanded.append(current)
            continue
        for option in match.group(1).split(","):
            variants.append(current[: match.start()] + option + current[match.end():])
    expanded.extend(variants)
    result: List[str] = []
    for item in expanded:
        for candidate in (item, item.replace("**/", "")):
            if candidate not in result:
                result.append(candidate)
    return result
