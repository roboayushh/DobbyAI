"""PRD 3-5 contract conformance: PRD example payloads validate, schemas never drift."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

import harness.contracts.execution as execution
import harness.contracts.queue as queue
import harness.contracts.verification as verification

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((Path(__file__).parent / "prd_examples").glob("*.json"))


def _model(name: str):
    for module in (execution, verification, queue):
        if hasattr(module, name):
            return getattr(module, name)
    raise AssertionError(f"No contract named {name}")


def test_every_prd_example_is_present() -> None:
    assert len(EXAMPLES) == 32


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_prd_example_validates(path: Path) -> None:
    model = _model(path.stem)
    parsed = model.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert parsed.schema_version == "1.0"


def test_publication_is_never_authorized_in_results() -> None:
    for name in ("QueueFinalResultV1", "ReleaseCandidateHandoffV1"):
        data = json.loads((Path(__file__).parent / "prd_examples" / f"{name}.json").read_text())
        data["publication_authorized"] = True
        with pytest.raises(Exception):
            _model(name).model_validate(data)


def _generator():
    spec = importlib.util.spec_from_file_location("generate_schemas", ROOT / "scripts" / "generate_schemas.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("relative", sorted(_generator().SCHEMAS))
def test_checked_in_schema_matches_contract(relative: str) -> None:
    model = _generator().SCHEMAS[relative]
    expected = json.dumps(TypeAdapter(model).json_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    actual = (ROOT / "schemas" / "v1" / relative).read_text(encoding="utf-8")
    assert actual == expected, f"{relative} is stale; run `python scripts/generate_schemas.py`"
