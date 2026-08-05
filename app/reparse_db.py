"""Backfill missing extraction JSON without leaking Postgres connections."""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from app.database import create_pool_with_retry, ensure_database_schema
from app.gemini_parser import parse_message


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ReparseDB")

DATABASE_URL = os.environ.get("DATABASE_URL")


async def reparse() -> tuple[int, int]:
    """Backfill JSON, leaving every row in its existing review/queue state."""
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set in .env")
        return 0, 1

    logger.info("Connecting to DB...")
    pool = await create_pool_with_retry(DATABASE_URL)
    if pool is None:
        return 0, 1

    try:
        if not await ensure_database_schema(pool):
            return 0, 1

        # Fetch before calling Gemini: no database connection is held while a
        # remote LLM request is in flight.
        async with pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT e.id AS ext_id, m.text AS original_text
                FROM extractions e
                JOIN messages m ON e.message_id = m.id AND e.chat_id = m.chat_id
                WHERE e.sync_status = 'pending' AND e.raw_json IS NULL
                ORDER BY m.id DESC
                """
            )

        if not records:
            logger.info("No empty pending records found. Everything is already parsed.")
            return 0, 0

        logger.info("Found %s records to re-parse. Starting...", len(records))
        success_count = 0
        error_count = 0
        skipped_count = 0

        for idx, record in enumerate(records, 1):
            ext_id = record["ext_id"]
            text = record["original_text"]
            logger.info("[%s/%s] Parsing extraction ID: %s...", idx, len(records), ext_id)

            try:
                parsed_data = await parse_message(text)
                if parsed_data.get("error"):
                    logger.error("Gemini error for ID %s: %s", ext_id, parsed_data.get("error"))
                    error_count += 1
                    await asyncio.sleep(1.5)
                    continue

                if not parsed_data.get("is_relevant"):
                    # Do not persist an empty/non-listing payload.  Leaving it
                    # pending would repeatedly spend Gemini calls; storing it
                    # as raw_json would let a later approval send invalid data
                    # to the sync queue.  `failed` is an explicit, manual-review
                    # terminal state and is never claimed by sync_job.
                    async with pool.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE extractions
                            SET sync_status = 'failed',
                                sync_last_error = $1,
                                sync_next_retry_at = NULL
                            WHERE id = $2
                              AND sync_status = 'pending'
                              AND raw_json IS NULL
                            """,
                            "Reparse skipped: parser marked message as not relevant",
                            ext_id,
                        )
                    skipped_count += 1
                    logger.info("Skipped irrelevant extraction ID: %s", ext_id)
                    await asyncio.sleep(1.5)
                    continue

                json_str = json.dumps(parsed_data)
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE extractions
                        SET raw_json = $1
                        WHERE id = $2
                          AND sync_status = 'pending'
                          AND raw_json IS NULL
                        """,
                        json_str,
                        ext_id,
                    )
                success_count += 1
                logger.info("Saved JSON for ID: %s", ext_id)
            except Exception as exc:
                logger.error("Failed to parse ID %s: %s", ext_id, exc)
                error_count += 1

            # Stay below the provider rate limit without holding a DB connection.
            await asyncio.sleep(1.5)

        logger.info(
            "Reparse complete. fixed=%s skipped_irrelevant=%s errors=%s",
            success_count,
            skipped_count,
            error_count,
        )
        return success_count, error_count
    finally:
        try:
            await pool.close()
        except Exception as exc:
            logger.warning("Failed to close reparse database pool: %s", exc)


if __name__ == "__main__":
    _success, _errors = asyncio.run(reparse())
    raise SystemExit(1 if _errors else 0)
