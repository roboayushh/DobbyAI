"""Single-controller leases bound to an orchestration lifecycle version."""
from __future__ import annotations

import datetime
import hashlib
import secrets
from dataclasses import dataclass

from harness.persistence import RunStore


class LeaseUnavailableError(RuntimeError):
    code = "RUN_LEASE_UNAVAILABLE"


class StaleLeaseError(RuntimeError):
    code = "STALE_RUN_LEASE"


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner_id: str
    token: str
    lifecycle_version: int
    expires_at: str


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class RunLeaseService:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def acquire(self, run_id: str, owner_id: str, ttl_seconds: int = 30) -> Lease:
        if ttl_seconds < 5 or ttl_seconds > 300:
            raise ValueError("lease TTL must be between 5 and 300 seconds")
        now = _utc_now()
        expires = now + datetime.timedelta(seconds=ttl_seconds)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.run_store.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lifecycle = conn.execute(
                "SELECT version FROM h_run_lifecycle WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not lifecycle:
                conn.rollback()
                raise KeyError(f"Lifecycle not initialized for run: {run_id}")
            existing = conn.execute(
                "SELECT * FROM h_orchestration_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing and datetime.datetime.fromisoformat(existing["expires_at"]) > now:
                conn.rollback()
                raise LeaseUnavailableError(
                    f"Run {run_id} already has an active controller lease"
                )
            conn.execute("DELETE FROM h_orchestration_leases WHERE run_id = ?", (run_id,))
            conn.execute(
                """
                INSERT INTO h_orchestration_leases(
                    run_id, owner_id, lease_token_sha256, acquired_at,
                    heartbeat_at, expires_at, lifecycle_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    owner_id,
                    digest,
                    now.isoformat(),
                    now.isoformat(),
                    expires.isoformat(),
                    lifecycle["version"],
                ),
            )
            conn.commit()
        return Lease(run_id, owner_id, token, lifecycle["version"], expires.isoformat())

    def renew(self, lease: Lease, ttl_seconds: int = 30) -> Lease:
        now = _utc_now()
        expires = now + datetime.timedelta(seconds=ttl_seconds)
        digest = hashlib.sha256(lease.token.encode("utf-8")).hexdigest()
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute(
                    """
                    UPDATE h_orchestration_leases
                    SET heartbeat_at = ?, expires_at = ?
                    WHERE run_id = ? AND owner_id = ? AND lease_token_sha256 = ?
                      AND expires_at > ?
                    """,
                    (
                        now.isoformat(),
                        expires.isoformat(),
                        lease.run_id,
                        lease.owner_id,
                        digest,
                        now.isoformat(),
                    ),
                ).rowcount
        if changed != 1:
            raise StaleLeaseError("Cannot renew missing, expired, or replaced lease")
        return Lease(
            lease.run_id,
            lease.owner_id,
            lease.token,
            lease.lifecycle_version,
            expires.isoformat(),
        )

    def assert_valid(self, lease: Lease) -> None:
        digest = hashlib.sha256(lease.token.encode("utf-8")).hexdigest()
        now = _utc_now().isoformat()
        with self.run_store.get_connection() as conn:
            row = conn.execute(
                """
                SELECT l.*, r.version AS current_version
                FROM h_orchestration_leases l
                JOIN h_run_lifecycle r ON r.run_id = l.run_id
                WHERE l.run_id = ? AND l.owner_id = ? AND l.lease_token_sha256 = ?
                """,
                (lease.run_id, lease.owner_id, digest),
            ).fetchone()
        if not row or row["expires_at"] <= now:
            raise StaleLeaseError("Lease is missing, expired, or replaced")
        if row["current_version"] < row["lifecycle_version"]:
            raise StaleLeaseError("Lifecycle version regressed below lease version")

    def release(self, lease: Lease) -> None:
        digest = hashlib.sha256(lease.token.encode("utf-8")).hexdigest()
        with self.run_store.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    DELETE FROM h_orchestration_leases
                    WHERE run_id = ? AND owner_id = ? AND lease_token_sha256 = ?
                    """,
                    (lease.run_id, lease.owner_id, digest),
                )

    def break_expired(self, run_id: str) -> bool:
        with self.run_store.get_connection() as conn:
            with conn:
                changed = conn.execute(
                    "DELETE FROM h_orchestration_leases WHERE run_id = ? AND expires_at <= ?",
                    (run_id, _utc_now().isoformat()),
                ).rowcount
        return changed == 1

