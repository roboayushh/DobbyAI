"""PRD 4: candidate-bound verification, validator overlay, completion gate, repair."""

from .comparator import CheckEvidence, ComparisonResult, compare
from .completion_gate import GateDecision, GateInputs, evaluate
from .contract_service import ContractBuilder, test_set_sha256
from .diff_review import DiffScopeReviewer, parse_git_diff
from .parsers import parse_exit_code, parse_pytest_junit, parse_unittest
from .service import VerificationBudgetDefaults, VerificationError, VerificationOutcome, VerificationService

__all__ = [
    "CheckEvidence",
    "ComparisonResult",
    "ContractBuilder",
    "DiffScopeReviewer",
    "GateDecision",
    "GateInputs",
    "VerificationBudgetDefaults",
    "VerificationError",
    "VerificationOutcome",
    "VerificationService",
    "compare",
    "evaluate",
    "parse_exit_code",
    "parse_git_diff",
    "parse_pytest_junit",
    "parse_unittest",
    "test_set_sha256",
]
