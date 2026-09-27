"""PRD 6 evaluator boundary: adapters translate external requests into RunRequestV1."""
from .interface import EvaluatorInputError
from .native_json import NativeJsonEvaluatorAdapter
from .official_adapter_placeholder import OfficialAdapterPlaceholder

__all__ = ["EvaluatorInputError", "NativeJsonEvaluatorAdapter", "OfficialAdapterPlaceholder"]
