"""Durable, review-gated export queue from Postgres to Airtable.

The queue uses short database transactions only to claim and finish work.  No
database connection is held while Airtable is called, so concurrent workers
never share an ``asyncpg.Connection`` and a stalled HTTP request cannot block
the queue's transactional connection.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Mapping, Optional
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

from app.airtable_client import upsert_developer, upsert_project, upsert_unit
from app.database import (
    approve_pending_extraction,
    close_db,
    create_pool_with_retry,
    ensure_database_schema,
    init_db,
)
from app.single_instance import acquire


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("SyncJob")

DATABASE_URL = os.environ.get("DATABASE_URL")


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default


# Default to safety when the setting is absent.  Production writes still need
# an explicit DRY_RUN=0 deployment setting.
DRY_RUN = _bool_env("DRY_RUN", True)


class PayloadValidationError(ValueError):
    """The stored extraction cannot be safely represented in Airtable."""


class AirtableWriteError(RuntimeError):
    """An Airtable upsert reported no durable record ID."""


@dataclass
class SyncResult:
    claimed: int = 0
    synced: int = 0
    dry_run_checked: int = 0
    retried: int = 0
    failed: int = 0
    validation_errors: int = 0
    lost_claims: int = 0
    database_error: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(
            self.database_error
            or self.retried
            or self.failed
            or self.validation_errors
            or self.lost_claims
        )

    @property
    def exit_code(self) -> int:
        return 1 if self.has_errors else 0


# The CTE locks only eligible rows, atomically switches them to processing, and
# returns the payload in one statement.  Another sync process cannot see a
# claimed row; abandoned leases become eligible again after expiry.
CLAIM_RECORDS_SQL = """
    WITH candidates AS (
        SELECT id
        FROM extractions
        WHERE raw_json IS NOT NULL
          AND needs_human IS NOT TRUE
          AND retry_count < $3
          AND NOT (id = ANY($5::INTEGER[]))
          AND created_at <= $6
          AND (
                (sync_status = 'pending'
                 AND (sync_next_retry_at IS NULL OR sync_next_retry_at <= NOW()))
             OR (sync_status = 'processing'
                 AND (sync_lease_until IS NULL OR sync_lease_until < NOW()))
          )
        ORDER BY created_at ASC, id ASC
        FOR UPDATE SKIP LOCKED
        LIMIT $1
    )
    UPDATE extractions AS extraction
    SET sync_status = 'processing',
        sync_claim_token = $2,
        sync_claimed_at = NOW(),
        sync_lease_until = NOW() + ($4::INTEGER * INTERVAL '1 second')
    FROM candidates
    WHERE extraction.id = candidates.id
    RETURNING extraction.id, extraction.raw_json, extraction.retry_count;
"""

RENEW_LEASE_SQL = """
    UPDATE extractions
    SET sync_lease_until = NOW() + ($3::INTEGER * INTERVAL '1 second')
    WHERE id = $1
      AND sync_status = 'processing'
      AND sync_claim_token = $2
    RETURNING id;
"""

MARK_SYNCED_SQL = """
    UPDATE extractions
    SET sync_status = 'synced',
        sync_claim_token = NULL,
        sync_claimed_at = NULL,
        sync_lease_until = NULL,
        sync_next_retry_at = NULL,
        sync_last_error = NULL,
        synced_at = NOW()
    WHERE id = $1
      AND sync_status = 'processing'
      AND sync_claim_token = $2
    RETURNING id;
"""

RELEASE_DRY_RUN_SQL = """
    UPDATE extractions
    SET sync_status = 'pending',
        sync_claim_token = NULL,
        sync_claimed_at = NULL,
        sync_lease_until = NULL
    WHERE id = $1
      AND sync_status = 'processing'
      AND sync_claim_token = $2
    RETURNING id;
"""

MARK_FAILURE_SQL = """
    UPDATE extractions
    SET retry_count = retry_count + 1,
        sync_status = CASE
            WHEN retry_count + 1 >= $3 THEN 'failed'
            ELSE 'pending'
        END,
        sync_next_retry_at = CASE
            WHEN retry_count + 1 >= $3 THEN NULL
            ELSE NOW() + ($4::DOUBLE PRECISION * INTERVAL '1 second')
        END,
        sync_last_error = $5,
        sync_claim_token = NULL,
        sync_claimed_at = NULL,
        sync_lease_until = NULL
    WHERE id = $1
      AND sync_status = 'processing'
      AND sync_claim_token = $2
    RETURNING sync_status, retry_count;
"""


def _value(record: Any, key: str, default: Any = None) -> Any:
    """Read fields from asyncpg.Record and plain dicts used by unit tests."""
    try:
        return record[key]
    except (KeyError, TypeError, IndexError):
        return default


def _decode_payload(raw_json: Any) -> dict:
    if isinstance(raw_json, str):
        raw_json = json.loads(raw_json)
    if not isinstance(raw_json, dict):
        raise PayloadValidationError("raw_json must be a JSON object")
    return raw_json


def _validate_payload(payload: Mapping[str, Any]) -> None:
    """Reject malformed payloads before any external write is attempted."""
    for key in ("Developer", "Projects"):
        value = payload.get(key)
        if value is not None and not isinstance(value, Mapping):
            raise PayloadValidationError(f"{key} must be an object")

    units = payload.get("Units", [])
    if units is None:
        units = []
    if not isinstance(units, list):
        raise PayloadValidationError("Units must be an array")
    if any(not isinstance(unit, Mapping) for unit in units):
        raise PayloadValidationError("every Units item must be an object")

    gaps = payload.get("Gaps", [])
    if gaps is not None and not isinstance(gaps, list):
        raise PayloadValidationError("Gaps must be an array")

    developer = payload.get("Developer") or {}
    project = payload.get("Projects") or {}
    has_developer = bool(developer.get("Developer"))
    has_project = bool(project.get("Project Name"))
    has_unit = any(unit.get("Unit type") or unit.get("Bedrooms") for unit in units)
    if not (has_developer or has_project or has_unit):
        raise PayloadValidationError("payload contains no developer, project, or unit to sync")
    if has_unit and not has_project:
        raise PayloadValidationError("units require a named project to avoid orphan records")


async def _sync_payload(payload: Mapping[str, Any]) -> None:
    """Run ordered Airtable upserts and fail if any requested write did not stick."""
    _validate_payload(payload)
    gaps = list(payload.get("Gaps") or [])
    dev_id = None
    project_id = None

    dev_data = payload.get("Developer") or {}
    if dev_data.get("Developer"):
        dev_id = await upsert_developer(dict(dev_data))
        if not dev_id:
            raise AirtableWriteError("Developer upsert returned no record ID")

    project_data = payload.get("Projects") or {}
    if project_data.get("Project Name"):
        project_id = await upsert_project(dict(project_data), dev_id, gaps)
        if not project_id:
            raise AirtableWriteError("Project upsert returned no record ID")

    project_name = project_data.get("Project Name")
    for unit in payload.get("Units") or []:
        if not (unit.get("Unit type") or unit.get("Bedrooms")):
            continue
        if not project_id or not project_name:
            raise PayloadValidationError("unit cannot be linked to a project")
        unit_id = await upsert_unit(dict(unit), project_id, project_name, gaps)
        if not unit_id:
            raise AirtableWriteError("Unit upsert returned no record ID")


async def _claim_records(
    db_pool: Any,
    *,
    batch_size: int,
    claim_token: str,
    max_retries: int,
    lease_seconds: int,
    exclude_ids: tuple[int, ...] = (),
    created_before: Optional[datetime] = None,
) -> list[Any]:
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            CLAIM_RECORDS_SQL,
            batch_size,
            claim_token,
            max_retries,
            lease_seconds,
            list(exclude_ids),
            created_before or datetime.now(timezone.utc),
        )


async def _renew_lease(
    db_pool: Any, record_id: int, claim_token: str, lease_seconds: int
) -> bool:
    async with db_pool.acquire() as conn:
        renewed = await conn.fetchval(
            RENEW_LEASE_SQL, record_id, claim_token, lease_seconds
        )
    return renewed is not None


async def _mark_synced(db_pool: Any, record_id: int, claim_token: str) -> bool:
    async with db_pool.acquire() as conn:
        updated = await conn.fetchval(MARK_SYNCED_SQL, record_id, claim_token)
    return updated is not None


async def _release_dry_run(db_pool: Any, record_id: int, claim_token: str) -> bool:
    async with db_pool.acquire() as conn:
        released = await conn.fetchval(RELEASE_DRY_RUN_SQL, record_id, claim_token)
    return released is not None


async def _mark_failure(
    db_pool: Any,
    *,
    record_id: int,
    claim_token: str,
    max_retries: int,
    retry_count: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    error: Exception,
) -> Optional[str]:
    """Release a failed claim and schedule its bounded exponential retry."""
    delay = min(retry_max_seconds, retry_base_seconds * (2 ** max(0, retry_count)))
    error_text = f"{type(error).__name__}: {error}"[:4000]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            MARK_FAILURE_SQL,
            record_id,
            claim_token,
            max_retries,
            delay,
            error_text,
        )
    return _value(row, "sync_status") if row is not None else None


async def _lease_heartbeat(
    db_pool: Any,
    *,
    record_id: int,
    claim_token: str,
    lease_seconds: int,
    lease_lost: asyncio.Event,
) -> None:
    """Renew a lease while a potentially slow Airtable write is in progress."""
    # Leases shorter than 30 seconds are rejected by the caller; this interval
    # leaves enough room for a retry before expiration.
    interval = max(1.0, lease_seconds / 3)
    try:
        while True:
            await asyncio.sleep(interval)
            renewed = await _renew_lease(db_pool, record_id, claim_token, lease_seconds)
            if not renewed:
                lease_lost.set()
                logger.error("Lost sync lease for record %s; stopping external work.", record_id)
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Continuing after a failed renewal can make two workers write the same
        # record, so treat a DB renewal failure as a lost lease.
        lease_lost.set()
        logger.error("Unable to renew sync lease for record %s: %s", record_id, exc)


async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _process_record(
    db_pool: Any,
    record: Any,
    *,
    claim_token: str,
    dry_run: bool,
    max_retries: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    lease_seconds: int,
    airtable_lock: asyncio.Lock,
) -> str:
    """Process one already-claimed record and return a stable outcome label."""
    record_id = _value(record, "id")
    retry_count = int(_value(record, "retry_count", 0) or 0)

    if dry_run:
        try:
            payload = _decode_payload(_value(record, "raw_json"))
            _validate_payload(payload)
            logger.info("DRY_RUN validated record %s; it remains pending.", record_id)
            outcome = "dry_run_checked"
        except (json.JSONDecodeError, PayloadValidationError) as exc:
            # Dry-run never increases retry_count or marks data failed: it is a
            # non-destructive diagnostic mode.
            logger.error("DRY_RUN validation failed for record %s: %s", record_id, exc)
            outcome = "validation_error"

        try:
            if not await _release_dry_run(db_pool, record_id, claim_token):
                logger.error("Could not release DRY_RUN claim for record %s.", record_id)
                return "lost_claim"
        except Exception as exc:
            logger.error("Could not release DRY_RUN claim for record %s: %s", record_id, exc)
            return "lost_claim"
        return outcome

    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _lease_heartbeat(
            db_pool,
            record_id=record_id,
            claim_token=claim_token,
            lease_seconds=lease_seconds,
            lease_lost=lease_lost,
        )
    )
    try:
        payload = _decode_payload(_value(record, "raw_json"))
        _validate_payload(payload)

        # Airtable helpers maintain an in-process cache.  Serialize their
        # updates even when several records are claimed concurrently, while DB
        # status operations still use independent pooled connections.
        async with airtable_lock:
            if lease_lost.is_set():
                return "lost_claim"
            await _sync_payload(payload)

        if lease_lost.is_set() or not await _mark_synced(db_pool, record_id, claim_token):
            logger.error("Could not mark record %s synced because its claim was lost.", record_id)
            return "lost_claim"
        logger.info("Successfully synced record %s.", record_id)
        return "synced"
    except (json.JSONDecodeError, PayloadValidationError, AirtableWriteError) as exc:
        logger.error("Sync validation/write failure for record %s: %s", record_id, exc)
        if lease_lost.is_set():
            return "lost_claim"
        try:
            status = await _mark_failure(
                db_pool,
                record_id=record_id,
                claim_token=claim_token,
                max_retries=max_retries,
                retry_count=retry_count,
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
                error=exc,
            )
        except Exception as mark_exc:
            logger.error("Could not record sync failure for %s: %s", record_id, mark_exc)
            return "lost_claim"
        return "failed" if status == "failed" else "retried" if status == "pending" else "lost_claim"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Unexpected sync failure for record %s", record_id)
        if lease_lost.is_set():
            return "lost_claim"
        try:
            status = await _mark_failure(
                db_pool,
                record_id=record_id,
                claim_token=claim_token,
                max_retries=max_retries,
                retry_count=retry_count,
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
                error=exc,
            )
        except Exception as mark_exc:
            logger.error("Could not record sync failure for %s: %s", record_id, mark_exc)
            return "lost_claim"
        return "failed" if status == "failed" else "retried" if status == "pending" else "lost_claim"
    finally:
        await _stop_heartbeat(heartbeat)


def _add_outcome(result: SyncResult, outcome: str) -> None:
    if outcome == "synced":
        result.synced += 1
    elif outcome == "dry_run_checked":
        result.dry_run_checked += 1
    elif outcome == "retried":
        result.retried += 1
    elif outcome == "failed":
        result.failed += 1
    elif outcome == "validation_error":
        result.validation_errors += 1
    else:
        result.lost_claims += 1


async def sync_pending_extractions(
    *,
    database_url: Optional[str] = None,
    dry_run: Optional[bool] = None,
    batch_size: Optional[int] = None,
    concurrency: Optional[int] = None,
) -> SyncResult:
    """Claim, export, and finalize every currently eligible extraction.

    Only human-approved rows (``needs_human = FALSE``) are eligible.  Failed
    rows are retained for explicit inspection; retryable failures receive a
    delayed next-attempt timestamp instead of being immediately re-claimed.
    """
    result = SyncResult()
    database_url = database_url or DATABASE_URL or os.environ.get("DATABASE_URL")
    active_dry_run = DRY_RUN if dry_run is None else dry_run
    batch_size = max(1, int(batch_size or _int_env("SYNC_BATCH_SIZE", 50)))
    concurrency = max(1, int(concurrency or _int_env("SYNC_CONCURRENCY", 1)))
    max_retries = _int_env("SYNC_MAX_RETRIES", 3)
    lease_seconds = _int_env("SYNC_LEASE_SECONDS", 900, minimum=30)
    retry_base_seconds = _float_env("SYNC_RETRY_BASE_SECONDS", 30.0, minimum=1.0)
    retry_max_seconds = _float_env("SYNC_RETRY_MAX_SECONDS", 3600.0, minimum=1.0)

    if not database_url:
        logger.error("DATABASE_URL is not set; sync cannot start.")
        result.database_error = True
        return result

    logger.info("Starting Airtable sync job (dry_run=%s).", active_dry_run)
    db_pool = await create_pool_with_retry(
        database_url,
        min_size=1,
        max_size=max(2, concurrency + 1),
    )
    if db_pool is None:
        result.database_error = True
        return result

    try:
        if not await ensure_database_schema(db_pool):
            result.database_error = True
            return result

        claim_token = str(uuid4())
        run_started_at = datetime.now(timezone.utc)
        airtable_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(concurrency)
        checked_in_dry_run: set[int] = set()

        async def process_with_limit(record: Any) -> str:
            async with semaphore:
                return await _process_record(
                    db_pool,
                    record,
                    claim_token=claim_token,
                    dry_run=active_dry_run,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                    retry_max_seconds=retry_max_seconds,
                    lease_seconds=lease_seconds,
                    airtable_lock=airtable_lock,
                )

        while True:
            records = await _claim_records(
                db_pool,
                batch_size=batch_size,
                claim_token=claim_token,
                max_retries=max_retries,
                lease_seconds=lease_seconds,
                exclude_ids=tuple(checked_in_dry_run) if active_dry_run else (),
                created_before=run_started_at,
            )
            if not records:
                break

            result.claimed += len(records)
            if active_dry_run:
                checked_in_dry_run.update(int(_value(record, "id")) for record in records)
            outcomes = await asyncio.gather(*(process_with_limit(record) for record in records))
            for outcome in outcomes:
                _add_outcome(result, outcome)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Sync job aborted by a database/queue error: %s", exc)
        result.database_error = True
    finally:
        try:
            await db_pool.close()
        except Exception as exc:
            logger.warning("Failed to close sync-job database pool: %s", exc)

    logger.info(
        "Sync complete. claimed=%s synced=%s dry_run_checked=%s retried=%s failed=%s "
        "validation_errors=%s lost_claims=%s db_error=%s",
        result.claimed,
        result.synced,
        result.dry_run_checked,
        result.retried,
        result.failed,
        result.validation_errors,
        result.lost_claims,
        result.database_error,
    )
    return result


async def _approve_from_cli(extraction_id: int) -> int:
    """Approve one reviewed row without starting an Airtable export."""
    if not await init_db():
        return 1
    try:
        return 0 if await approve_pending_extraction(extraction_id) else 1
    finally:
        await close_db()


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely sync reviewed Postgres extractions to Airtable.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate one claim batch and return every row to pending without Airtable writes.",
    )
    parser.add_argument(
        "--approve",
        type=int,
        metavar="EXTRACTION_ID",
        help="Explicitly approve one pending extraction, then exit without syncing Airtable.",
    )
    args = parser.parse_args()

    if args.approve is not None:
        return asyncio.run(_approve_from_cli(args.approve))

    # A local process lock provides a fast operator-facing failure.  Postgres
    # claims remain the source of truth across different hosts/containers.
    acquire("sync_job")
    result = asyncio.run(sync_pending_extractions(dry_run=True if args.dry_run else None))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
