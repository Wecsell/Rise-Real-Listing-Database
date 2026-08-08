"""Postgres lifecycle and persistence helpers.

The Telegram listener depends on Postgres for durable intake.  A failed
connection therefore must be visible to callers; quietly continuing without a
pool turns an otherwise recoverable outage into data loss.
"""

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Optional

try:
    import asyncpg
except ImportError:  # Allows static/unit tests in environments without asyncpg.
    asyncpg = None


logger = logging.getLogger("Database")
DATABASE_URL = os.environ.get("DATABASE_URL")

# The listener owns this pool.  Batch jobs intentionally create and close their
# own pools so their lifecycle cannot invalidate a long-lived listener pool.
pool = None


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive integer setting without making a bad env value fatal."""
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a non-negative float setting without making a bad env value fatal."""
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Temporary MVP switch: the operator-facing review command from
# app.database.approve_pending_extraction() does not exist yet (only a CLI
# flag on sync_job.py does), so gating every extraction behind needs_human
# would stall the pipeline entirely. While this is set, new extractions skip
# the review queue and sync straight through. Unset it once a real review UI
# ships — see the P0 chat-filter fix for why fail-closed matters here too.
MVP_SKIP_HUMAN_REVIEW = _bool_env("MVP_SKIP_HUMAN_REVIEW", False)


async def create_pool_with_retry(
    database_url: Optional[str] = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
    attempts: Optional[int] = None,
    retry_base_seconds: Optional[float] = None,
    retry_max_seconds: Optional[float] = None,
    connect_timeout_seconds: Optional[float] = None,
):
    """Create a Postgres pool with bounded exponential retry/backoff.

    ``None`` is returned only after all attempts have failed.  This lets an
    entry point fail closed instead of pretending that durable persistence is
    available.  The helper is also used by one-shot jobs, which must always
    close the returned pool themselves.
    """
    if asyncpg is None:
        logger.error("asyncpg module is not installed; PostgreSQL is unavailable.")
        return None

    database_url = database_url or DATABASE_URL or os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set; PostgreSQL is unavailable.")
        return None

    attempts = attempts if attempts is not None else _int_env("DB_CONNECT_ATTEMPTS", 5)
    retry_base_seconds = (
        retry_base_seconds
        if retry_base_seconds is not None
        else _float_env("DB_CONNECT_RETRY_BASE_SECONDS", 1.0)
    )
    retry_max_seconds = (
        retry_max_seconds
        if retry_max_seconds is not None
        else _float_env("DB_CONNECT_RETRY_MAX_SECONDS", 15.0)
    )
    connect_timeout_seconds = (
        connect_timeout_seconds
        if connect_timeout_seconds is not None
        else _float_env("DB_CONNECT_TIMEOUT_SECONDS", 10.0, minimum=0.1)
    )
    attempts = max(1, int(attempts))
    retry_base_seconds = max(0.0, float(retry_base_seconds))
    retry_max_seconds = max(0.0, float(retry_max_seconds))
    connect_timeout_seconds = max(0.1, float(connect_timeout_seconds))

    for attempt in range(1, attempts + 1):
        try:
            candidate = await asyncio.wait_for(
                asyncpg.create_pool(
                    database_url,
                    min_size=max(1, min_size),
                    max_size=max(max(1, min_size), max_size),
                ),
                timeout=connect_timeout_seconds,
            )
            logger.info("Postgres pool connected (attempt %s/%s).", attempt, attempts)
            return candidate
        except Exception as exc:
            if attempt >= attempts:
                logger.error(
                    "Postgres connection failed after %s attempt(s): %s", attempts, exc
                )
                break

            delay = min(retry_max_seconds, retry_base_seconds * (2 ** (attempt - 1)))
            logger.warning(
                "Postgres connection attempt %s/%s failed: %s. Retrying in %.1fs.",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    return None


async def _run_migrations(conn: Any) -> None:
    """Bring pre-existing databases to the durable sync-queue schema."""
    await conn.execute(
        """
        ALTER TABLE extractions
            ADD COLUMN IF NOT EXISTS raw_json JSONB,
            ADD COLUMN IF NOT EXISTS sync_status VARCHAR(50),
            ADD COLUMN IF NOT EXISTS retry_count INTEGER,
            ADD COLUMN IF NOT EXISTS sync_claim_token VARCHAR(64),
            ADD COLUMN IF NOT EXISTS sync_claimed_at TIMESTAMP WITH TIME ZONE,
            ADD COLUMN IF NOT EXISTS sync_lease_until TIMESTAMP WITH TIME ZONE,
            ADD COLUMN IF NOT EXISTS sync_next_retry_at TIMESTAMP WITH TIME ZONE,
            ADD COLUMN IF NOT EXISTS sync_last_error TEXT,
            ADD COLUMN IF NOT EXISTS synced_at TIMESTAMP WITH TIME ZONE,
            ADD COLUMN IF NOT EXISTS needs_human BOOLEAN DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS ingest_key VARCHAR(64);
        """
    )
    # Old rows were created before the queue state existed.  Unknown state and
    # missing human-review values are conservatively made pending/review-needed.
    await conn.execute(
        """
        UPDATE extractions
        SET sync_status = 'pending'
        WHERE sync_status IS NULL
           OR sync_status NOT IN ('pending', 'processing', 'synced', 'failed');

        UPDATE extractions
        SET retry_count = 0
        WHERE retry_count IS NULL OR retry_count < 0;

        UPDATE extractions
        SET needs_human = TRUE
        WHERE needs_human IS NULL;

        ALTER TABLE extractions
            ALTER COLUMN sync_status SET DEFAULT 'pending',
            ALTER COLUMN sync_status SET NOT NULL,
            ALTER COLUMN retry_count SET DEFAULT 0,
            ALTER COLUMN retry_count SET NOT NULL,
            ALTER COLUMN needs_human SET DEFAULT TRUE,
            ALTER COLUMN needs_human SET NOT NULL;

        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'extractions_sync_status_check'
                  AND conrelid = 'extractions'::regclass
            ) THEN
                ALTER TABLE extractions
                    ADD CONSTRAINT extractions_sync_status_check
                    CHECK (sync_status IN ('pending', 'processing', 'synced', 'failed'));
            END IF;
        END
        $$;

        CREATE INDEX IF NOT EXISTS idx_extractions_sync_ready
            ON extractions (sync_status, sync_next_retry_at, sync_lease_until, created_at, id)
            WHERE raw_json IS NOT NULL AND needs_human IS FALSE;

        CREATE INDEX IF NOT EXISTS idx_extractions_source_lookup
            ON extractions (message_id, chat_id, slot);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_extractions_ingest_key
            ON extractions (ingest_key)
            WHERE ingest_key IS NOT NULL;
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_classifications (
            file_id VARCHAR(255) PRIMARY KEY,
            doc_type VARCHAR(50) NOT NULL,
            classified_by VARCHAR(50) NOT NULL,
            model_used VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """
    )


async def ensure_database_schema(db_pool: Any) -> bool:
    """Apply idempotent schema migrations to an already-created pool."""
    try:
        async with db_pool.acquire() as conn:
            await _run_migrations(conn)
        return True
    except Exception as exc:
        logger.error("Postgres migrations failed; database is not ready: %s", exc)
        return False


async def init_db() -> bool:
    """Initialise the listener's pool and migrations, returning readiness."""
    global pool

    if pool is not None:
        if await check_db_ping():
            return True
        logger.warning("Existing Postgres pool failed health check; reconnecting.")
        await close_db()

    candidate = await create_pool_with_retry()
    if candidate is None:
        return False

    if not await ensure_database_schema(candidate):
        try:
            await candidate.close()
        except Exception as close_exc:
            logger.warning("Failed to close unusable Postgres pool: %s", close_exc)
        return False

    pool = candidate
    logger.info("Postgres database is ready.")
    return True


async def check_db_ping() -> bool:
    """Perform a real ``SELECT 1`` health check against PostgreSQL."""
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            val = await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2.0)
            return val == 1
    except Exception as exc:
        logger.error("DB ping failed: %s", exc)
        return False


async def save_message(msg_id: int, chat_id: int, sender_id: int, text: str, has_media: bool) -> bool:
    """Persist a raw Telegram message and report whether it reached Postgres."""
    if pool is None:
        logger.warning("Postgres unavailable; message %s from chat %s was not saved.", msg_id, chat_id)
        return False
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO messages (id, chat_id, sender_id, text, has_media)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id, chat_id) DO NOTHING
                """,
                msg_id,
                chat_id,
                sender_id or 0,
                text,
                has_media,
            )
        return True
    except Exception as exc:
        logger.error("Error saving message %s (chat %s): %s", msg_id, chat_id, exc)
        return False


async def save_extraction(
    message_id: int,
    chat_id: int,
    project_recid: str,
    object_guess: str,
    confidence: float,
    slot: str,
    url_status: str,
    why: str,
    needs_human: bool,
    raw_json: Optional[dict] = None,
) -> bool:
    """Persist one extraction in a review-first, pending queue state."""
    if pool is None:
        logger.warning(
            "Postgres unavailable; extraction [%s] for %r (project %s) was not saved.",
            slot,
            object_guess,
            project_recid,
        )
        return False
    try:
        project_recid = (project_recid or "")[:255]
        object_guess = (object_guess or "")[:255]
        url_status = (url_status or "")[:50]
        slot = (slot or "")[:50]
        # A missing value must never become SQL NULL and evade the review gate.
        needs_human = True if needs_human is None else bool(needs_human)
        if MVP_SKIP_HUMAN_REVIEW and needs_human:
            logger.info(
                "MVP_SKIP_HUMAN_REVIEW=1: extraction [%s] for %r (project %s) skips human review.",
                slot, object_guess, project_recid,
            )
            needs_human = False
        json_str = json.dumps(raw_json) if raw_json is not None else None
        ingest_key = _extraction_ingest_key(
            message_id=message_id,
            chat_id=chat_id,
            slot=slot,
            object_guess=object_guess,
            why=why,
            raw_json=raw_json,
        )

        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO extractions (
                    message_id, chat_id, project_recid, object_guess, confidence,
                    slot, url_status, why, needs_human, raw_json, sync_status,
                    ingest_key
                )
                SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending', $11
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM extractions AS existing
                    WHERE existing.message_id = $1
                      AND existing.chat_id = $2
                      AND existing.slot = $6
                      AND (
                            existing.slot IN ('group_bio', 'history_backfill', 'realtime')
                         OR (
                                existing.object_guess IS NOT DISTINCT FROM $4
                            AND existing.why IS NOT DISTINCT FROM $8
                            AND existing.raw_json IS NOT DISTINCT FROM $10::jsonb
                         )
                      )
                )
                ON CONFLICT (ingest_key) WHERE ingest_key IS NOT NULL DO NOTHING
                """,
                message_id,
                chat_id,
                project_recid,
                object_guess,
                confidence,
                slot,
                url_status,
                why,
                needs_human,
                json_str,
                ingest_key,
            )
        if result.endswith("0"):
            logger.info(
                "Duplicate extraction for message %s/chat %s/slot %s was already persisted.",
                message_id,
                chat_id,
                slot,
            )
        return True
    except Exception as exc:
        logger.error("Error saving extraction for message %s: %s", message_id, exc)
        return False


def _extraction_ingest_key(
    *,
    message_id: int,
    chat_id: int,
    slot: str,
    object_guess: str,
    why: str,
    raw_json: Optional[dict],
) -> str:
    """Return a deterministic idempotency key for one extraction source.

    A historical or realtime message is one source event regardless of a later
    model wording change, so rescans cannot build an unbounded review queue.
    Link-derived slots may legitimately produce several records for the same
    Telegram message; their payload metadata is therefore included in the key.
    """
    identity: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "slot": slot,
    }
    if slot not in {"group_bio", "history_backfill", "realtime"}:
        identity.update(
            object_guess=object_guess,
            why=why,
            raw_json=raw_json,
        )
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def approve_pending_extraction(extraction_id: int) -> bool:
    """Explicitly release one reviewed pending extraction for Airtable sync.

    This intentionally does not approve failed or in-flight rows: retry and
    lease recovery must remain separate from a human's data-quality decision.
    """
    if pool is None:
        logger.warning("Postgres unavailable; extraction %s was not approved.", extraction_id)
        return False
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE extractions
                SET needs_human = FALSE
                WHERE id = $1
                  AND sync_status = 'pending'
                  AND needs_human IS TRUE
                """,
                extraction_id,
            )
        approved = result.endswith("1")
        if not approved:
            logger.warning(
                "Extraction %s was not approved: it must be pending and awaiting review.",
                extraction_id,
            )
        return approved
    except Exception as exc:
        logger.error("Error approving extraction %s: %s", extraction_id, exc)
        return False


async def save_fact(
    project_recid: str,
    old_value: str,
    new_value: str,
    fact_type: str = "price_change",
    unit_id: Optional[str] = None,
    source_message_id: Optional[int] = None,
    model_used: Optional[str] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
) -> bool:
    """Persist a historical fact and report any failure instead of hiding it."""
    if pool is None:
        logger.warning(
            "Postgres unavailable; fact [%s] for project %s (%s -> %s) was not saved.",
            fact_type,
            project_recid,
            old_value,
            new_value,
        )
        return False
    try:
        old_str = "" if old_value is None else str(old_value)
        new_str = "" if new_value is None else str(new_value)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO facts (
                    project_recid, unit_id, old_value, new_value, fact_type,
                    source_message_id, model_used, tokens_in, tokens_out
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                project_recid[:255] if project_recid else "",
                unit_id,
                old_str,
                new_str,
                fact_type[:50] if fact_type else "price_change",
                source_message_id,
                model_used,
                tokens_in,
                tokens_out,
            )
        logger.info("Fact recorded [%s] for project %s: %s -> %s", fact_type, project_recid, old_value, new_value)
        return True
    except Exception as exc:
        logger.error("Error saving fact for project %s: %s", project_recid, exc)
        return False


async def close_db() -> None:
    """Close and clear the listener pool even if closing itself errors."""
    global pool
    current_pool = pool
    pool = None
    if current_pool is None:
        return
    logger.info("Closing database pool...")
    try:
        await current_pool.close()
    except Exception as exc:
        logger.warning("Error closing database pool: %s", exc)
