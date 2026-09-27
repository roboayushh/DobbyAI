"""Read-only file inventory and pinned Tree-sitter symbol extraction."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

from harness.persistence import ArtifactStore, RunStore, canonical_json


PARSER_VERSION = "tree-sitter@0.23.2;python@0.23.6;javascript@0.23.1;typescript@0.23.2"
QUERY_VERSION = "prd2-symbol-query@1.0"

IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "coverage",
        ".coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        ".next",
        ".nuxt",
    }
)

# Machine-generated files: they match almost any identifier and would crowd real code out
# of every packet, so they are not model-visible evidence.
GENERATED_FILENAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "poetry.lock", "pipfile.lock", "cargo.lock", "composer.lock", "gemfile.lock", "go.sum", "uv.lock",
})
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map")

SECRET_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "service-account.json",
}

SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[opusr]_[A-Za-z0-9]{30,}"),
    re.compile(
        rb"(?i)(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{16,}['\"]"
    ),
)

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mts": "typescript",
    ".cts": "typescript",
}

TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".rb",
    ".php",
}

INSTRUCTION_NAMES = {
    "agents.md",
    "claude.md",
    "copilot-instructions.md",
    ".cursorrules",
}


@dataclass(frozen=True)
class InventoryItem:
    relative_path: str
    full_path: Path
    content: Optional[bytes]
    content_sha256: str
    byte_size: int
    language: Optional[str]
    parse_state: str
    exclusion_reason: Optional[str]


@dataclass(frozen=True)
class IndexBuildResult:
    run_id: str
    source_revision: str
    indexed_files: int
    excluded_files: int
    symbol_count: int
    reused: bool
    repository_map_artifact_id: Optional[str]


class RepositoryIndexer:
    def __init__(
        self,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        data_root: str | Path,
        *,
        max_file_bytes: int = 512 * 1024,
        max_symbols_per_file: int = 10_000,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root).resolve()
        self.max_file_bytes = max_file_bytes
        self.max_symbols_per_file = max_symbols_per_file
        self._parsers = self._build_parsers()
        # PRD 3: when a writable task workspace is active, index and verify
        # evidence against it instead of the read-only PRD 1 baseline.
        self.root_override: Optional[Any] = None

    @staticmethod
    def _build_parsers() -> Dict[str, Parser]:
        languages = {
            "python": Language(tree_sitter_python.language()),
            "javascript": Language(tree_sitter_javascript.language()),
            "typescript": Language(tree_sitter_typescript.language_typescript()),
            "tsx": Language(tree_sitter_typescript.language_tsx()),
        }
        return {name: Parser(language) for name, language in languages.items()}

    def workspace_root(self, run_id: str) -> Path:
        if self.root_override is not None:
            override = self.root_override(run_id)
            if override is not None:
                return Path(override)
        workspace = self.run_store.get_workspace(run_id)
        if not workspace or workspace["state"] != "READY" or workspace["writable"] != 0:
            raise ValueError("Prepared workspace is missing or not read-only")
        root = (self.data_root / workspace["worktree_relpath"]).resolve()
        expected_parent = (self.data_root / "runs" / run_id).resolve()
        try:
            root.relative_to(expected_parent)
        except ValueError as exc:
            raise ValueError("Workspace escapes the prepared run root") from exc
        if not root.is_dir():
            raise ValueError("Prepared workspace directory is missing")
        return root

    def build(
        self, run_id: str, source_revision: str, *, reuse_from_revision: Optional[str] = None
    ) -> IndexBuildResult:
        root = self.workspace_root(run_id)
        inventory = self._inventory(root)
        if self._can_reuse(run_id, source_revision, inventory):
            return self._result_from_db(run_id, source_revision, reused=True)
        reusable = self._reusable_rows(run_id, reuse_from_revision) if reuse_from_revision else {}

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows: List[Tuple[Any, ...]] = []
        symbols: List[Tuple[Any, ...]] = []
        for item in inventory:
            file_index_id = f"fidx_{uuid.uuid4().hex[:16]}"
            parse_state = item.parse_state
            parser_name: Optional[str] = None
            parser_version: Optional[str] = None
            item_symbols: List[Dict[str, Any]] = []
            exclusion_reason = item.exclusion_reason
            previous = reusable.get(item.relative_path)
            if (
                previous is not None
                and previous["content_sha256"] == item.content_sha256
                and previous["parse_state"] == ("PARSED" if item.language in self._parsers else item.parse_state)
            ):
                # Unchanged file: copy parse results instead of re-parsing.
                parse_state = previous["parse_state"]
                parser_name = previous["parser_name"]
                parser_version = previous["parser_version"]
                exclusion_reason = previous["exclusion_reason"]
                item_symbols = previous["symbols"]
            elif item.content is not None and item.language in self._parsers:
                parser_name = f"tree-sitter-{item.language}"
                parser_version = PARSER_VERSION
                item_symbols, parse_error = self._extract_symbols(item.content, item.language)
                if parse_error:
                    parse_state = "TEXT_ONLY"
                    exclusion_reason = "parse_error_text_fallback"
                    item_symbols = []
                else:
                    parse_state = "PARSED"
            rows.append(
                (
                    file_index_id,
                    run_id,
                    source_revision,
                    item.relative_path,
                    item.content_sha256,
                    item.byte_size,
                    item.language,
                    parser_name,
                    parser_version,
                    parse_state,
                    exclusion_reason,
                    now,
                )
            )
            for symbol in item_symbols[: self.max_symbols_per_file]:
                symbols.append(
                    (
                        f"sym_{uuid.uuid4().hex[:16]}",
                        file_index_id,
                        symbol["kind"],
                        symbol["name"],
                        symbol.get("signature"),
                        symbol["start_line"],
                        symbol["end_line"],
                        symbol["content_sha256"],
                    )
                )

        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM h_file_index WHERE run_id = ? AND source_revision = ?",
                (run_id, source_revision),
            )
            conn.executemany(
                """
                INSERT INTO h_file_index(
                    file_index_id, run_id, source_revision, relative_path,
                    content_sha256, byte_size, language, parser_name, parser_version,
                    parse_state, exclusion_reason, valid, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                rows,
            )
            conn.executemany(
                """
                INSERT INTO h_symbols(
                    symbol_id, file_index_id, symbol_kind, qualified_name,
                    signature, start_line, end_line, content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                symbols,
            )
            conn.commit()

        repository_map = {
            "schema_version": "1.0",
            "run_id": run_id,
            "source_revision": source_revision,
            "parser_version": PARSER_VERSION,
            "query_version": QUERY_VERSION,
            "files": [
                {
                    "path": item.relative_path,
                    "sha256": item.content_sha256,
                    "bytes": item.byte_size,
                    "language": item.language,
                    "state": rows[index][9],
                    "exclusion_reason": rows[index][10],
                    "trust": (
                        "untrusted_repository_instruction"
                        if Path(item.relative_path).name.lower() in INSTRUCTION_NAMES
                        else "untrusted_repository_content"
                    ),
                }
                for index, item in enumerate(inventory)
            ],
            "symbol_count": len(symbols),
        }
        map_sha = hashlib.sha256(canonical_json(repository_map).encode("utf-8")).hexdigest()
        artifact_path = (
            f"prd2/index/{source_revision[:16]}-{hashlib.sha256(PARSER_VERSION.encode()).hexdigest()[:8]}"
            f"/repository-map-{map_sha[:16]}.json"
        )
        self.artifact_store.write_json(
            run_id,
            artifact_path,
            repository_map,
            "repository_map",
        )
        artifact = self.artifact_store.get_artifact_by_path(run_id, artifact_path)
        return IndexBuildResult(
            run_id=run_id,
            source_revision=source_revision,
            indexed_files=sum(1 for item in inventory if item.parse_state != "EXCLUDED"),
            excluded_files=sum(1 for item in inventory if item.parse_state == "EXCLUDED"),
            symbol_count=len(symbols),
            reused=False,
            repository_map_artifact_id=artifact["artifact_id"] if artifact else None,
        )

    def _reusable_rows(self, run_id: str, revision: str) -> Dict[str, Dict[str, Any]]:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM h_file_index
                WHERE run_id = ? AND source_revision = ? AND valid = 1
                  AND (parser_version IS NULL OR parser_version = ?)
                """,
                (run_id, revision, PARSER_VERSION),
            ).fetchall()
            symbol_rows = conn.execute(
                """
                SELECT s.*, f.relative_path FROM h_symbols s
                JOIN h_file_index f ON f.file_index_id = s.file_index_id
                WHERE f.run_id = ? AND f.source_revision = ? AND f.valid = 1
                """,
                (run_id, revision),
            ).fetchall()
        symbols: Dict[str, List[Dict[str, Any]]] = {}
        for row in symbol_rows:
            symbols.setdefault(row["relative_path"], []).append(
                {
                    "kind": row["symbol_kind"],
                    "name": row["qualified_name"],
                    "signature": row["signature"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "content_sha256": row["content_sha256"],
                }
            )
        return {
            row["relative_path"]: {**dict(row), "symbols": symbols.get(row["relative_path"], [])}
            for row in rows
        }

    def status(self, run_id: str) -> Dict[str, Any]:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS files,
                       SUM(CASE WHEN parse_state = 'EXCLUDED' THEN 1 ELSE 0 END) AS excluded,
                       SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid
                FROM h_file_index WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            symbol_count = conn.execute(
                """
                SELECT COUNT(*) FROM h_symbols s
                JOIN h_file_index f ON f.file_index_id = s.file_index_id
                WHERE f.run_id = ? AND f.valid = 1
                """,
                (run_id,),
            ).fetchone()[0]
        return {
            "run_id": run_id,
            "files": row["files"] or 0,
            "excluded": row["excluded"] or 0,
            "valid": row["valid"] or 0,
            "symbols": symbol_count,
            "parser_version": PARSER_VERSION,
            "query_version": QUERY_VERSION,
        }

    def _can_reuse(
        self, run_id: str, source_revision: str, inventory: Sequence[InventoryItem]
    ) -> bool:
        with self.run_store.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT relative_path, content_sha256, byte_size, valid,
                       COALESCE(parser_version, '') AS parser_version
                FROM h_file_index
                WHERE run_id = ? AND source_revision = ?
                ORDER BY relative_path
                """,
                (run_id, source_revision),
            ).fetchall()
        if len(rows) != len(inventory):
            return False
        for row, item in zip(rows, inventory):
            if (
                row["relative_path"] != item.relative_path
                or row["content_sha256"] != item.content_sha256
                or row["byte_size"] != item.byte_size
                or row["valid"] != 1
            ):
                return False
            if item.language in self._parsers and row["parser_version"] != PARSER_VERSION:
                return False
        return bool(rows)

    def _result_from_db(
        self, run_id: str, source_revision: str, *, reused: bool
    ) -> IndexBuildResult:
        with self.run_store.get_connection() as conn:
            counts = conn.execute(
                """
                SELECT COUNT(*) AS files,
                       SUM(CASE WHEN parse_state = 'EXCLUDED' THEN 1 ELSE 0 END) AS excluded
                FROM h_file_index WHERE run_id = ? AND source_revision = ?
                """,
                (run_id, source_revision),
            ).fetchone()
            symbol_count = conn.execute(
                """
                SELECT COUNT(*) FROM h_symbols s
                JOIN h_file_index f ON f.file_index_id = s.file_index_id
                WHERE f.run_id = ? AND f.source_revision = ? AND f.valid = 1
                """,
                (run_id, source_revision),
            ).fetchone()[0]
        return IndexBuildResult(
            run_id,
            source_revision,
            (counts["files"] or 0) - (counts["excluded"] or 0),
            counts["excluded"] or 0,
            symbol_count,
            reused,
            None,
        )

    def _inventory(self, root: Path) -> List[InventoryItem]:
        items: List[InventoryItem] = []
        for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            for filename in sorted(files):
                full_path = Path(current_root) / filename
                relative = full_path.relative_to(root).as_posix()
                if full_path.is_symlink():
                    target = os.readlink(full_path).encode("utf-8", errors="replace")
                    items.append(
                        InventoryItem(
                            relative,
                            full_path,
                            None,
                            hashlib.sha256(target).hexdigest(),
                            len(target),
                            None,
                            "EXCLUDED",
                            "symlink_not_model_visible",
                        )
                    )
                    continue
                size = full_path.stat().st_size
                if filename.lower() in GENERATED_FILENAMES or filename.lower().endswith(GENERATED_SUFFIXES):
                    items.append(
                        InventoryItem(
                            relative,
                            full_path,
                            None,
                            self._hash_file(full_path),
                            size,
                            None,
                            "EXCLUDED",
                            "generated_or_minified",
                        )
                    )
                    continue
                if filename.lower() in SECRET_FILENAMES or filename.lower().endswith(
                    (".pem", ".key", ".p12", ".pfx")
                ):
                    items.append(
                        InventoryItem(
                            relative,
                            full_path,
                            None,
                            self._hash_file(full_path),
                            size,
                            None,
                            "EXCLUDED",
                            "secret_like_path",
                        )
                    )
                    continue
                if size > self.max_file_bytes:
                    items.append(
                        InventoryItem(
                            relative,
                            full_path,
                            None,
                            self._hash_file(full_path),
                            size,
                            None,
                            "EXCLUDED",
                            "model_visible_file_too_large",
                        )
                    )
                    continue
                content = full_path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if b"\x00" in content:
                    items.append(
                        InventoryItem(relative, full_path, None, digest, size, None, "EXCLUDED", "binary")
                    )
                    continue
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    items.append(
                        InventoryItem(
                            relative,
                            full_path,
                            None,
                            digest,
                            size,
                            None,
                            "EXCLUDED",
                            "invalid_utf8",
                        )
                    )
                    continue
                if any(pattern.search(content) for pattern in SECRET_PATTERNS):
                    items.append(
                        InventoryItem(
                            relative,
                            full_path,
                            None,
                            digest,
                            size,
                            None,
                            "EXCLUDED",
                            "high_confidence_secret",
                        )
                    )
                    continue
                if filename.endswith((".min.js", ".min.css")) or re.search(
                    rb"(?i)@(generated|auto-generated)|do not edit", content[:2048]
                ):
                    items.append(
                        InventoryItem(
                            relative,
                            full_path,
                            None,
                            digest,
                            size,
                            None,
                            "EXCLUDED",
                            "generated_or_minified",
                        )
                    )
                    continue
                extension = full_path.suffix.lower()
                language = LANGUAGES.get(extension)
                text_allowed = language is not None or extension in TEXT_EXTENSIONS or filename in {
                    "Dockerfile",
                    "Makefile",
                }
                items.append(
                    InventoryItem(
                        relative,
                        full_path,
                        content if text_allowed else None,
                        digest,
                        size,
                        language,
                        "TEXT_ONLY" if text_allowed else "EXCLUDED",
                        None if text_allowed else "unsupported_text_type",
                    )
                )
        return sorted(items, key=lambda item: item.relative_path)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _extract_symbols(
        self, content: bytes, language: str
    ) -> Tuple[List[Dict[str, Any]], bool]:
        tree = self._parsers[language].parse(content)
        if tree.root_node.has_error:
            return [], True
        symbols: List[Dict[str, Any]] = []
        definition_ranges: set[Tuple[int, int]] = set()
        definition_types = {
            "python": {
                "function_definition": "function",
                "class_definition": "class",
            },
            "javascript": {
                "function_declaration": "function",
                "class_declaration": "class",
                "method_definition": "method",
            },
            "typescript": {
                "function_declaration": "function",
                "class_declaration": "class",
                "method_definition": "method",
                "interface_declaration": "interface",
                "type_alias_declaration": "type",
                "enum_declaration": "enum",
            },
            "tsx": {
                "function_declaration": "function",
                "class_declaration": "class",
                "method_definition": "method",
                "interface_declaration": "interface",
                "type_alias_declaration": "type",
                "enum_declaration": "enum",
            },
        }[language]

        def text(node: Node) -> str:
            return content[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

        def visit(node: Node, scope: Tuple[str, ...]) -> None:
            next_scope = scope
            kind = definition_types.get(node.type)
            if kind:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = text(name_node)
                    qualified = ".".join((*scope, name))
                    definition_ranges.add((name_node.start_byte, name_node.end_byte))
                    node_text = text(node)
                    signature = node_text.splitlines()[0][:300]
                    symbols.append(
                        {
                            "kind": kind,
                            "name": qualified,
                            "signature": signature,
                            "start_line": node.start_point.row + 1,
                            "end_line": node.end_point.row + 1,
                            "content_sha256": hashlib.sha256(
                                content[node.start_byte : node.end_byte]
                            ).hexdigest(),
                        }
                    )
                    if kind in {"class", "function", "method"}:
                        next_scope = (*scope, name)
            elif node.type in {"import_statement", "import_from_statement"}:
                import_text = " ".join(text(node).split())[:300]
                symbols.append(
                    {
                        "kind": "import",
                        "name": import_text,
                        "signature": import_text,
                        "start_line": node.start_point.row + 1,
                        "end_line": node.end_point.row + 1,
                        "content_sha256": hashlib.sha256(
                            content[node.start_byte : node.end_byte]
                        ).hexdigest(),
                    }
                )
            for child in node.children:
                visit(child, next_scope)

        visit(tree.root_node, ())

        def references(node: Node) -> None:
            if len(symbols) >= self.max_symbols_per_file:
                return
            if node.type in {"identifier", "property_identifier", "type_identifier"}:
                byte_range = (node.start_byte, node.end_byte)
                if byte_range not in definition_ranges:
                    name = text(node)
                    if name and len(name) <= 200:
                        symbols.append(
                            {
                                "kind": "reference",
                                "name": name,
                                "signature": None,
                                "start_line": node.start_point.row + 1,
                                "end_line": node.end_point.row + 1,
                                "content_sha256": hashlib.sha256(
                                    content[node.start_byte : node.end_byte]
                                ).hexdigest(),
                            }
                        )
            for child in node.children:
                references(child)

        references(tree.root_node)
        # Deterministic de-duplication protects the DB from repeated identifiers
        # on the same line while retaining distinct locations.
        unique: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
        for symbol in symbols:
            key = (symbol["kind"], symbol["name"], symbol["start_line"])
            unique.setdefault(key, symbol)
        return list(unique.values()), False
