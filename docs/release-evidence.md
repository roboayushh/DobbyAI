# Release evidence

`harness release evidence` (or `make release-evidence`) evaluates every release gate in
PRD 6 §15.3 from recorded evidence and prints `QUALIFIED` or `NOT_QUALIFIED`. A gate
that was not executed is `NOT_RUN`, never `PASS`.

| Gate | Evidence it reads | How to produce it |
|---|---|---|
| `contracts_migrations`, `source_safety`, `containment`, `verification`, `queue_git`, `evaluator`, `export`, `plugins` | `release-evidence/tests/junit.xml`. Named tests must have run and passed; skipped counts as `NOT_RUN`. | `make test` (Docker running) |
| `model` | `release-evidence/live/*.json` from a **prescribed** model profile. `claude-bridge` runs and baseline records never count. | `scripts/live_eval.py` with `HARNESS_MODEL_PROFILE=deepseek` or `qwen` |
| `comparative_evaluation` | `release-evidence/comparison/summary-<profile>.json` with `prescribed_model_evidence: true` | `scripts/compare_eval.py run cases.json --profile deepseek` |
| `setup_clean_machine` | `release-evidence/clean-machine/*.json` | `scripts/clean_machine.sh` |
| `documentation` | README and `docs/{prd6-gap-report,configuration,evaluator,security,limitations,provenance,profiles}.md` | — |
| `provenance_licenses` | SBOM, notices, both lock files | `make sbom` |
| `secret_scan` | every tracked and untracked (non-ignored) file | — |
| `optional_apply_publish` | not required: P1 disabled | — |

## Live end-to-end evaluation

`scripts/live_eval.py` sends each case through the exact headless evaluator path
(`harness run --input … --non-interactive`) and scores it like a SWE-bench task:

1. The repository contains a real, injected defect. The request carries only a
   symptom description; the harness is not told which file or test is involved.
2. The exported `patch.diff` is applied to a clean copy of the original repository.
3. The project's **original test files are restored**, so no configuration can pass by
   editing the oracle.
4. The project's full test suite (the "hidden tests") runs in a fresh
   `python:3.12-slim` container, before and after the patch. New failures are counted
   as regressions.

`scripts/baseline_loop.py` is the PRD 6 §15.1 single-role shell-loop baseline. It uses
the same model profile and adapter, the same sampling settings, tasks, starting
commits, runtime, call and wall budgets, and the same scorer.
`scripts/compare_eval.py` runs both configurations for N trials and writes
`release-evidence/comparison/summary-<profile>.{json,md}`, keeping development cases
(used while tuning) separate from held-out cases (never used for tuning).

To produce prescribed-model evidence:

```bash
export AI_API_KEY=sk-...   HARNESS_MODEL_PROFILE=deepseek     # or qwen
.venv/bin/python scripts/compare_eval.py run cases.json --work /tmp/cmp --profile deepseek --trials 2
harness release evidence
```

`cases.json` is a list of `{name, split, repo, issue, hidden_install, hidden_test}`.

## Current recorded state

See [prd6-gap-report.md](prd6-gap-report.md#release-evidence) for the gate results at
release time, and the development-bridge comparison numbers.
