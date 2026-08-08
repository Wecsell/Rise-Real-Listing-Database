"""Regression tests for safe re-parsing of legacy extraction rows."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import app.reparse_db as reparse_db


class _TrackingAcquire:
    def __init__(self, pool, conn):
        self.pool = pool
        self.conn = conn

    async def __aenter__(self):
        self.pool.active_connections += 1
        return self.conn

    async def __aexit__(self, *args):
        self.pool.active_connections -= 1
        return False


class _Conn:
    def __init__(self):
        self.executed = []
        self.records = [{"ext_id": 15, "original_text": "unrelated chatter"}]

    async def fetch(self, _query):
        return self.records

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.active_connections = 0
        self.closed = False

    def acquire(self):
        return _TrackingAcquire(self, self.conn)

    async def close(self):
        self.closed = True


class TestReparseIrrelevantPayload(IsolatedAsyncioTestCase):
    async def test_irrelevant_payload_becomes_terminal_without_raw_json(self):
        conn = _Conn()
        pool = _Pool(conn)

        async def irrelevant_parser(_text):
            # The initial SELECT connection must already have been returned
            # before a slow/remote Gemini request begins.
            self.assertEqual(pool.active_connections, 0)
            return {"is_relevant": False, "reason": "noise"}

        with (
            patch.object(reparse_db, "DATABASE_URL", "postgresql://test"),
            patch.object(reparse_db, "create_pool_with_retry", AsyncMock(return_value=pool)),
            patch.object(reparse_db, "ensure_database_schema", AsyncMock(return_value=True)),
            patch.object(reparse_db, "parse_message", irrelevant_parser),
            patch.object(reparse_db.asyncio, "sleep", AsyncMock()),
        ):
            success_count, error_count = await reparse_db.reparse()

        self.assertEqual((success_count, error_count), (0, 0))
        self.assertTrue(pool.closed)
        self.assertEqual(len(conn.executed), 1)
        query, args = conn.executed[0]
        self.assertIn("SET sync_status = 'failed'", query)
        self.assertNotIn("SET raw_json", query)
        self.assertIn("not relevant", args[0])
        self.assertEqual(args[1], 15)
