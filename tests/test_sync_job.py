"""Regression tests for the durable Postgres -> Airtable sync queue."""

import asyncio
from datetime import datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import app.sync_job as sync_job


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Conn:
    def __init__(self):
        self.fetch_calls = []
        self.fetchval_calls = []
        self.fetchrow_calls = []
        self.records = []
        self.fetchval_result = 1
        self.fetchrow_result = {"sync_status": "pending", "retry_count": 1}

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.records

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return self.fetchval_result

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_result


class _Pool:
    def __init__(self, *connections):
        self.connections = list(connections) or [_Conn()]
        self.acquire_count = 0
        self.closed = False

    def acquire(self):
        conn = self.connections[min(self.acquire_count, len(self.connections) - 1)]
        self.acquire_count += 1
        return _Acquire(conn)

    async def close(self):
        self.closed = True


class _AtomicClaimStore:
    def __init__(self):
        self.claimed = False


class _AtomicClaimConn(_Conn):
    def __init__(self, store):
        super().__init__()
        self.store = store

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if self.store.claimed:
            return []
        self.store.claimed = True
        return [{"id": 99, "raw_json": "{}", "retry_count": 0}]


class _DistinctConnectionPool:
    """Small pool fake that gives every concurrent acquire a distinct conn."""
    def __init__(self, store):
        self.store = store
        self.connections = []
        self.closed = False

    def acquire(self):
        conn = _AtomicClaimConn(self.store)
        self.connections.append(conn)
        return _Acquire(conn)

    async def close(self):
        self.closed = True


class TestClaimQuery(IsolatedAsyncioTestCase):
    async def test_claim_is_atomic_and_review_gated(self):
        conn = _Conn()
        conn.records = [{"id": 7, "raw_json": "{}", "retry_count": 0}]
        pool = _Pool(conn)

        records = await sync_job._claim_records(
            pool,
            batch_size=5,
            claim_token="claim-1",
            max_retries=3,
            lease_seconds=900,
        )

        self.assertEqual(records[0]["id"], 7)
        query, args = conn.fetch_calls[0]
        normalized = " ".join(query.lower().split())
        self.assertIn("for update skip locked", normalized)
        self.assertIn("needs_human is not true", normalized)
        self.assertIn("sync_status = 'processing'", normalized)
        self.assertIn("sync_lease_until is null or sync_lease_until < now()", normalized)
        self.assertEqual(args[:5], (5, "claim-1", 3, 900, []))
        self.assertIsInstance(args[5], datetime)

    async def test_two_workers_claim_a_row_only_once_with_distinct_connections(self):
        store = _AtomicClaimStore()
        pool = _DistinctConnectionPool(store)

        first, second = await asyncio.gather(
            sync_job._claim_records(
                pool,
                batch_size=1,
                claim_token="worker-a",
                max_retries=3,
                lease_seconds=900,
            ),
            sync_job._claim_records(
                pool,
                batch_size=1,
                claim_token="worker-b",
                max_retries=3,
                lease_seconds=900,
            ),
        )

        self.assertEqual(sorted(len(records) for records in (first, second)), [0, 1])
        self.assertEqual(len(pool.connections), 2)
        self.assertIsNot(pool.connections[0], pool.connections[1])
        for conn in pool.connections:
            self.assertIn("FOR UPDATE SKIP LOCKED", conn.fetch_calls[0][0])

    async def test_failure_releases_claim_with_backoff(self):
        conn = _Conn()
        pool = _Pool(conn)

        status = await sync_job._mark_failure(
            pool,
            record_id=9,
            claim_token="claim-9",
            max_retries=3,
            retry_count=1,
            retry_base_seconds=30,
            retry_max_seconds=3600,
            error=RuntimeError("Airtable unavailable"),
        )

        self.assertEqual(status, "pending")
        query, args = conn.fetchrow_calls[0]
        self.assertIn("sync_next_retry_at", query)
        self.assertEqual(args[:4], (9, "claim-9", 3, 60))
        self.assertIn("RuntimeError", args[4])


class TestDryRunSafety(IsolatedAsyncioTestCase):
    async def test_dry_run_release_restores_pending_status(self):
        conn = _Conn()
        pool = _Pool(conn)

        released = await sync_job._release_dry_run(pool, 10, "dry-10")

        self.assertTrue(released)
        query, args = conn.fetchval_calls[0]
        normalized = " ".join(query.lower().split())
        self.assertIn("set sync_status = 'pending'", normalized)
        self.assertNotIn("sync_status = 'synced'", normalized)
        self.assertEqual(args, (10, "dry-10"))

    async def test_dry_run_never_calls_airtable_and_releases_pending_claim(self):
        record = {
            "id": 11,
            "retry_count": 0,
            "raw_json": '{"Developer": {"Developer": "Safe Dev"}}',
        }
        release = AsyncMock(return_value=True)
        dev_upsert = AsyncMock(return_value="recDev")

        with (
            patch.object(sync_job, "_release_dry_run", release),
            patch.object(sync_job, "upsert_developer", dev_upsert),
            patch.object(sync_job, "upsert_project", AsyncMock()),
            patch.object(sync_job, "upsert_unit", AsyncMock()),
        ):
            outcome = await sync_job._process_record(
                _Pool(),
                record,
                claim_token="dry-claim",
                dry_run=True,
                max_retries=3,
                retry_base_seconds=30,
                retry_max_seconds=3600,
                lease_seconds=900,
                airtable_lock=asyncio.Lock(),
            )

        self.assertEqual(outcome, "dry_run_checked")
        # Explicit argument assertions keep this test independent of database
        # implementation details while proving the claim is returned pending.
        release.assert_awaited_once()
        self.assertEqual(release.await_args.args[1:], (11, "dry-claim"))
        dev_upsert.assert_not_awaited()

    async def test_invalid_dry_run_payload_is_still_released_not_marked_failed(self):
        release = AsyncMock(return_value=True)
        with patch.object(sync_job, "_release_dry_run", release):
            outcome = await sync_job._process_record(
                _Pool(),
                {"id": 12, "retry_count": 2, "raw_json": "not json"},
                claim_token="dry-invalid",
                dry_run=True,
                max_retries=3,
                retry_base_seconds=30,
                retry_max_seconds=3600,
                lease_seconds=900,
                airtable_lock=asyncio.Lock(),
            )

        self.assertEqual(outcome, "validation_error")
        self.assertEqual(release.await_args.args[1:], (12, "dry-invalid"))


class TestJobLifecycle(IsolatedAsyncioTestCase):
    async def test_claim_and_record_finalization_do_not_share_a_connection(self):
        store = _AtomicClaimStore()
        pool = _DistinctConnectionPool(store)
        # The first atomic claim returns a valid row; the next sees the lease
        # and returns no row.  Finalization must acquire a separate connection.
        original_fetch = _AtomicClaimConn.fetch

        async def fetch_with_valid_payload(self, query, *args):
            records = await original_fetch(self, query, *args)
            if records:
                records[0]["raw_json"] = '{"Developer": {"Developer": "Dev"}}'
            return records

        with (
            patch.object(_AtomicClaimConn, "fetch", fetch_with_valid_payload),
            patch.object(sync_job, "create_pool_with_retry", AsyncMock(return_value=pool)),
            patch.object(sync_job, "ensure_database_schema", AsyncMock(return_value=True)),
            patch.object(sync_job, "_sync_payload", AsyncMock()),
        ):
            result = await sync_job.sync_pending_extractions(
                database_url="postgresql://test",
                dry_run=False,
                batch_size=1,
                concurrency=1,
            )

        self.assertEqual(result.synced, 1)
        self.assertGreaterEqual(len(pool.connections), 3)
        self.assertEqual(len(pool.connections[0].fetch_calls), 1)
        self.assertEqual(len(pool.connections[1].fetchval_calls), 1)
        self.assertTrue(pool.closed)

    async def test_dry_run_checks_each_row_once_then_closes_pool(self):
        pool = _Pool()
        record = {"id": 21, "retry_count": 0, "raw_json": "{}"}
        claim = AsyncMock(side_effect=[[record], []])
        process = AsyncMock(return_value="dry_run_checked")

        with (
            patch.object(sync_job, "create_pool_with_retry", AsyncMock(return_value=pool)),
            patch.object(sync_job, "ensure_database_schema", AsyncMock(return_value=True)),
            patch.object(sync_job, "_claim_records", claim),
            patch.object(sync_job, "_process_record", process),
        ):
            result = await sync_job.sync_pending_extractions(
                database_url="postgresql://test",
                dry_run=True,
                batch_size=1,
                concurrency=1,
            )

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.dry_run_checked, 1)
        self.assertEqual(claim.await_count, 2)
        self.assertEqual(claim.await_args_list[0].kwargs["exclude_ids"], ())
        self.assertEqual(claim.await_args_list[1].kwargs["exclude_ids"], (21,))
        self.assertTrue(pool.closed)

    async def test_production_job_closes_its_own_pool_after_success(self):
        pool = _Pool()
        record = {"id": 22, "retry_count": 0, "raw_json": "{}"}
        claim = AsyncMock(side_effect=[[record], []])
        process = AsyncMock(return_value="synced")

        with (
            patch.object(sync_job, "create_pool_with_retry", AsyncMock(return_value=pool)),
            patch.object(sync_job, "ensure_database_schema", AsyncMock(return_value=True)),
            patch.object(sync_job, "_claim_records", claim),
            patch.object(sync_job, "_process_record", process),
        ):
            result = await sync_job.sync_pending_extractions(
                database_url="postgresql://test",
                dry_run=False,
                batch_size=1,
                concurrency=2,
            )

        self.assertEqual(result.synced, 1)
        self.assertEqual(claim.await_count, 2)
        self.assertTrue(pool.closed)
