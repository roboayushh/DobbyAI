"""The non-replaceable trusted kernel boundary for controller plugins (PRD 6 sections 1.1, 13.6-13.7).

A controller plugin returns ``TransitionProposal`` objects; only the kernel decides.
The built-in controller and any replacement receive identical decisions because
both go through the same checks here:

* lifecycle transitions must be in the fixed transition table, and only host
  verification may reach READY_FOR_REVIEW;
* actions are admitted only through the PRD 3 capability registry and policy
  (there is no raw host shell / Docker socket / network capability to request);
* model calls may only use the run's frozen prescribed profile and AI_API_KEY;
* external effects become capability requests (P1 effects are disabled) and are
  never dispatched without an exact approval grant;
* approvals cannot be created, granted, or consumed by a plugin (no port exists).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from harness.contracts import OrchestrationState
from harness.orchestration.lifecycle import ALLOWED_LIFECYCLE_TRANSITIONS
from harness.plugins.interfaces import KernelDecision, TransitionProposal

PLUGIN_VISIBLE_PORTS = ("state_view", "policy_decision", "budget_view", "artifact_reader", "submit_capability_request")


@dataclass
class KernelPorts:
    """What a plugin receives. Deliberately no DB connection, filesystem root, socket, key, or approval mutation."""

    state_view: Mapping[str, Any]
    budget_view: Mapping[str, Any]


class KernelGateway:
    def __init__(self, *, policy_engine=None, run_id: str = "", frozen_model_profile: Optional[str] = None,
                 capability_registry=None, p1_enabled: bool = False) -> None:
        self.policy_engine = policy_engine
        self.run_id = run_id
        self.frozen_model_profile = frozen_model_profile
        self.p1_enabled = p1_enabled
        if capability_registry is None:
            from harness.policy.capabilities import DEFAULT_REGISTRY

            capability_registry = DEFAULT_REGISTRY
        self.capability_registry = capability_registry

    def admit(self, proposal: TransitionProposal, state_view: Mapping[str, Any]) -> KernelDecision:
        if not isinstance(proposal, TransitionProposal):
            return KernelDecision(False, "PROPOSAL_INVALID", "Controllers must return TransitionProposal objects")
        handler = {
            "LIFECYCLE_TRANSITION": self._transition,
            "ACTION": self._action,
            "MODEL_CALL": self._model_call,
            "EFFECT": self._effect,
            "APPROVAL": self._approval,
        }.get(proposal.kind)
        if handler is None:
            return KernelDecision(False, "KERNEL_DENIED", f"Unknown proposal kind {proposal.kind!r}; raw host access is not a kernel capability")
        return handler(dict(proposal.payload), state_view)

    def _transition(self, payload: Dict[str, Any], state_view: Mapping[str, Any]) -> KernelDecision:
        try:
            current = OrchestrationState(state_view.get("state"))
            target = OrchestrationState(payload.get("to"))
        except ValueError:
            return KernelDecision(False, "TRANSITION_INVALID", "Unknown lifecycle state")
        if target not in ALLOWED_LIFECYCLE_TRANSITIONS.get(current, set()):
            return KernelDecision(False, "TRANSITION_DENIED", f"{current.value} -> {target.value} is not in the transition table")
        if target == OrchestrationState.READY_FOR_REVIEW and not state_view.get("host_completion_gate_pass"):
            return KernelDecision(False, "COMPLETION_AUTHORITY_DENIED", "Only the host completion gate can emit PASS")
        return KernelDecision(True, "ALLOWED")

    def _action(self, payload: Dict[str, Any], state_view: Mapping[str, Any]) -> KernelDecision:
        for name in payload.get("capabilities", []):
            try:
                self.capability_registry.get(name)
            except Exception:
                return KernelDecision(False, "CAPABILITY_UNKNOWN", f"{name} is not a registered sandbox capability")
        if payload.get("host") or payload.get("docker_socket") or payload.get("network"):
            return KernelDecision(False, "POLICY_DENIED", "Actions run only in the admitted network-less sandbox")
        return KernelDecision(True, "ADMISSION_REQUIRED", "Submit as a PRD 2 CODE proposal; PRD 3 policy admits or denies it")

    def _model_call(self, payload: Dict[str, Any], state_view: Mapping[str, Any]) -> KernelDecision:
        if payload.get("credential_env", "AI_API_KEY") != "AI_API_KEY" or payload.get("api_key"):
            return KernelDecision(False, "ONE_KEY_RULE", "All roles use the single host AI_API_KEY; no other credential")
        profile = payload.get("profile_id")
        if profile and self.frozen_model_profile and profile != self.frozen_model_profile:
            return KernelDecision(False, "ONE_MODEL_RULE", "The run's prescribed model profile is frozen; no substitution or fallback")
        return KernelDecision(True, "ALLOWED")

    def _effect(self, payload: Dict[str, Any], state_view: Mapping[str, Any]) -> KernelDecision:
        operation = payload.get("operation")
        if operation in ("APPLY_LOCAL", "PUSH_NEW_BRANCH", "CREATE_PULL_REQUEST", "MERGE_TARGET", "DELETE_REMOTE_BRANCH") and not self.p1_enabled:
            return KernelDecision(False, "CAPABILITY_DISABLED", f"{operation} is disabled in this release profile")
        if payload.get("force") or payload.get("refspec", "").startswith("+") or "*" in payload.get("refspec", ""):
            return KernelDecision(False, "PUBLICATION_RESTRICTED", "Force, wildcard, and delete refspecs are rejected")
        return KernelDecision(False, "PENDING_APPROVAL", "External effects require an exact user-owned approval grant")

    def _approval(self, payload: Dict[str, Any], state_view: Mapping[str, Any]) -> KernelDecision:
        return KernelDecision(False, "APPROVAL_PORT_UNAVAILABLE", "Plugins cannot create, grant, or consume approvals")
