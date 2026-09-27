"""Freeze one resolved model identity for the lifetime of a run."""
from __future__ import annotations

import datetime

from harness.model.profile import ResolvedProfile
from harness.persistence import RunStore, canonical_json


class ModelConfigConflictError(RuntimeError):
    code = "MODEL_PROFILE_CONFLICT"


class ModelConfigStore:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def freeze(
        self,
        run_id: str,
        resolved: ResolvedProfile,
        *,
        adapter_name: str,
        adapter_version: str,
    ) -> None:
        profile = resolved.contract
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM h_run_model_config WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing:
                if (
                    existing["profile_fingerprint"] != profile.profile_fingerprint
                    or existing["adapter_name"] != adapter_name
                    or existing["adapter_version"] != adapter_version
                ):
                    conn.rollback()
                    raise ModelConfigConflictError(
                        "A run cannot change model profile or adapter after orchestration starts"
                    )
                conn.commit()
                return
            conn.execute(
                """
                INSERT INTO h_run_model_config(
                    run_id, profile_id, protocol, endpoint_origin, model_id,
                    profile_fingerprint, adapter_name, adapter_version, tokenizer_id,
                    context_window_tokens, max_output_tokens, safety_margin_tokens,
                    sampling_json, credential_env_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AI_API_KEY', ?)
                """,
                (
                    run_id,
                    profile.profile_id,
                    profile.protocol,
                    profile.endpoint_origin,
                    profile.model,
                    profile.profile_fingerprint,
                    adapter_name,
                    adapter_version,
                    profile.tokenizer,
                    profile.context_window_tokens,
                    profile.max_output_tokens,
                    profile.safety_margin_tokens,
                    canonical_json(profile.sampling.model_dump(mode="json")),
                    now,
                ),
            )
            conn.commit()

    def get(self, run_id: str) -> dict:
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM h_run_model_config WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Model config is not frozen for run: {run_id}")
        return dict(row)
