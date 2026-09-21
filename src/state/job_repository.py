"""Durable lease fencing for periodic background jobs."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from src.core.database import Transaction

from .repository import Repository


@dataclass(frozen=True, slots=True)
class JobClaim:
    run_key: str
    job_name: str
    lease_token: str
    lease_expires_at: int
    attempts: int


class JobRepository(Repository):
    """Ensure one logical periodic-job slot runs at most one worker at a time."""

    async def claim(
        self,
        job_name: str,
        run_key: str,
        *,
        lease_seconds: int,
        now: int | None = None,
    ) -> JobClaim | None:
        clean_job_name = str(job_name).strip()
        clean_run_key = str(run_key).strip()
        if not 1 <= len(clean_job_name) <= 100:
            raise ValueError("job_name must contain 1 to 100 characters")
        if not 1 <= len(clean_run_key) <= 240:
            raise ValueError("run_key must contain 1 to 240 characters")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")

        claimed_at = int(time.time() if now is None else now)
        expires_at = claimed_at + int(lease_seconds)
        token = uuid.uuid4().hex

        def operation(transaction: Transaction) -> JobClaim | None:
            rows = transaction.read(
                """
                SELECT status, lease_expires_at, attempts
                FROM background_job_runs
                WHERE run_key = ?
                """,
                (clean_run_key,),
            )
            if not rows:
                transaction.write(
                    """
                    INSERT INTO background_job_runs (
                        run_key, job_name, status, attempts,
                        lease_owner, lease_expires_at, created_at, updated_at
                    ) VALUES (?, ?, 'claimed', 1, ?, ?, ?, ?)
                    """,
                    (
                        clean_run_key,
                        clean_job_name,
                        token,
                        expires_at,
                        claimed_at,
                        claimed_at,
                    ),
                )
                attempts = 1
            else:
                row = rows[0]
                if row["status"] == "completed":
                    return None
                current_expiry = int(row["lease_expires_at"] or 0)
                if current_expiry > claimed_at:
                    return None
                attempts = int(row["attempts"]) + 1
                result = transaction.write(
                    """
                    UPDATE background_job_runs
                    SET job_name = ?, attempts = ?, lease_owner = ?,
                        lease_expires_at = ?, last_error = NULL, updated_at = ?
                    WHERE run_key = ? AND status = 'claimed'
                      AND lease_expires_at <= ?
                    """,
                    (
                        clean_job_name,
                        attempts,
                        token,
                        expires_at,
                        claimed_at,
                        clean_run_key,
                        claimed_at,
                    ),
                )
                if result.rowcount != 1:
                    return None

            return JobClaim(
                run_key=clean_run_key,
                job_name=clean_job_name,
                lease_token=token,
                lease_expires_at=expires_at,
                attempts=attempts,
            )

        return await self.database.transaction(operation)

    async def mark_completed(
        self,
        claim: JobClaim,
        *,
        completed_at: int | None = None,
    ) -> bool:
        timestamp = int(time.time() if completed_at is None else completed_at)
        result = await self.database.write(
            """
            UPDATE background_job_runs
            SET status = 'completed', lease_owner = NULL,
                lease_expires_at = NULL, last_error = NULL,
                updated_at = ?, completed_at = ?
            WHERE run_key = ? AND status = 'claimed' AND lease_owner = ?
            """,
            (timestamp, timestamp, claim.run_key, claim.lease_token),
        )
        return result.rowcount == 1

    async def record_failure(
        self,
        claim: JobClaim,
        *,
        error: str,
        retry_at: int,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time() if now is None else now)
        if retry_at <= timestamp:
            raise ValueError("retry_at must be in the future")
        # Replace the token so a slow/stale worker cannot later mark this run
        # complete after it has already surrendered the claim.
        deferred_token = "deferred-" + uuid.uuid4().hex
        result = await self.database.write(
            """
            UPDATE background_job_runs
            SET lease_owner = ?, lease_expires_at = ?, last_error = ?, updated_at = ?
            WHERE run_key = ? AND status = 'claimed' AND lease_owner = ?
            """,
            (
                deferred_token,
                int(retry_at),
                str(error)[:1000],
                timestamp,
                claim.run_key,
                claim.lease_token,
            ),
        )
        return result.rowcount == 1

    async def cleanup_completed(self, *, before: int, limit: int = 1_000) -> int:
        bounded_limit = max(1, min(int(limit), 10_000))
        result = await self.database.write(
            """
            DELETE FROM background_job_runs
            WHERE run_key IN (
                SELECT run_key FROM background_job_runs
                WHERE status = 'completed' AND completed_at < ?
                ORDER BY completed_at
                LIMIT ?
            )
            """,
            (int(before), bounded_limit),
        )
        return max(0, result.rowcount)

    async def cleanup_abandoned(self, *, before: int, limit: int = 1_000) -> int:
        """Delete expired claims only after a generous operational retention window."""

        bounded_limit = max(1, min(int(limit), 10_000))
        result = await self.database.write(
            """
            DELETE FROM background_job_runs
            WHERE run_key IN (
                SELECT run_key FROM background_job_runs
                WHERE status = 'claimed' AND lease_expires_at < ?
                ORDER BY lease_expires_at, run_key
                LIMIT ?
            )
            """,
            (int(before), bounded_limit),
        )
        return max(0, result.rowcount)
