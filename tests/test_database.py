import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.database as db


class _FakeConn:
    def __init__(self, value=1, delay=0.0):
        self._value = value
        self._delay = delay

    async def fetchval(self, query):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._value


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class TestCheckDbPing(unittest.TestCase):
    """
    check_db_ping() питает healthcheck: is_healthy = tg_ok and tg_roundtrip and db_ok
    (app/listener.py). Существующие тесты healthcheck подставляют фейковый
    health_checker, поэтому саму эту функцию не вызывал ни один тест - из-за чего
    незамеченным жил NameError на неимпортированном asyncio: база отвечала
    нормально, а прод отдавал 500 на каждом опросе.
    """

    def setUp(self):
        self._orig_pool = db.pool

    def tearDown(self):
        db.pool = self._orig_pool

    def test_healthy_db_returns_true(self):
        db.pool = _FakePool(_FakeConn(value=1))
        self.assertTrue(asyncio.run(db.check_db_ping()))

    def test_no_pool_returns_false(self):
        db.pool = None
        self.assertFalse(asyncio.run(db.check_db_ping()))

    def test_unexpected_value_returns_false(self):
        db.pool = _FakePool(_FakeConn(value=0))
        self.assertFalse(asyncio.run(db.check_db_ping()))

    def test_slow_db_times_out_and_returns_false(self):
        """Зависшая база не должна держать healthcheck - на это и стоит timeout=2.0."""
        db.pool = _FakePool(_FakeConn(value=1, delay=5.0))
        self.assertFalse(asyncio.run(db.check_db_ping()))


class _FakeExecuteConn:
    def __init__(self, result="UPDATE 1"):
        self.executed = []
        self.result = result

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return self.result


class TestSilentLossWithoutPostgres(unittest.TestCase):
    """
    Находки без Postgres должны быть ВИДНЫ в логе, а не молча теряться.

    Регрессия 03.08.2026: save_extraction() возвращалась без единого лога при
    pool=None. Извлечение юнитов из шахматки Mångata успешно отработало
    ("Extracted 7 units from Google Sheet"), но ни строки не попало ни в
    Postgres, ни в Airtable - в логе не осталось следа потери вовсе.
    save_fact() ту же дыру уже закрывал раньше в этой сессии.
    """

    def setUp(self):
        self._orig_pool = db.pool

    def tearDown(self):
        db.pool = self._orig_pool

    def test_save_extraction_warns_and_does_not_raise_without_pool(self):
        db.pool = None
        with self.assertLogs('Database', level='WARNING') as log:
            asyncio.run(db.save_extraction(
                message_id=1, chat_id=1, project_recid="Mangata",
                object_guess="Unit 1 (2 BR)", confidence=0.95, slot="unit_price",
                url_status="parsed", why="Price: 100000$", needs_human=True,
            ))
        self.assertTrue(any("Mangata" in m for m in log.output))

    def test_save_extraction_writes_when_pool_is_present(self):
        conn = _FakeExecuteConn()
        db.pool = _FakePool(conn)
        asyncio.run(db.save_extraction(
            message_id=1, chat_id=1, project_recid="Mangata",
            object_guess="Unit 1 (2 BR)", confidence=0.95, slot="unit_price",
            url_status="parsed", why="Price: 100000$", needs_human=True,
        ))
        self.assertEqual(len(conn.executed), 1)
        query, args = conn.executed[0]
        self.assertIn("WHERE NOT EXISTS", query)
        self.assertIn("ON CONFLICT (ingest_key)", query)
        self.assertEqual(len(args[10]), 64)

    def test_history_rescan_key_is_stable_but_link_sources_stay_distinct(self):
        history_first = db._extraction_ingest_key(
            message_id=7,
            chat_id=11,
            slot="history_backfill",
            object_guess="history_backfill",
            why="first model phrasing",
            raw_json={"Projects": {"Project Name": "A"}},
        )
        history_second = db._extraction_ingest_key(
            message_id=7,
            chat_id=11,
            slot="history_backfill",
            object_guess="history_backfill",
            why="later model phrasing",
            raw_json={"Projects": {"Project Name": "B"}},
        )
        link_first = db._extraction_ingest_key(
            message_id=7,
            chat_id=11,
            slot="link_fetch",
            object_guess="Parsed from link: https://example.test/a",
            why="ok",
            raw_json={"Projects": {"Project Name": "A"}},
        )
        link_second = db._extraction_ingest_key(
            message_id=7,
            chat_id=11,
            slot="link_fetch",
            object_guess="Parsed from link: https://example.test/b",
            why="ok",
            raw_json={"Projects": {"Project Name": "A"}},
        )

        self.assertEqual(history_first, history_second)
        self.assertNotEqual(link_first, link_second)

    def test_save_fact_warns_and_does_not_raise_without_pool(self):
        db.pool = None
        with self.assertLogs('Database', level='WARNING') as log:
            asyncio.run(db.save_fact(
                project_recid="rec123", old_value="400000", new_value="430000",
                fact_type="price_change",
            ))
        self.assertTrue(any("rec123" in m for m in log.output))

    def test_save_fact_preserves_zero_as_a_real_value(self):
        """None -> '', но 0/'0' - тоже значение, а не отсутствие данных."""
        conn = _FakeExecuteConn()
        db.pool = _FakePool(conn)
        asyncio.run(db.save_fact(project_recid="rec123", old_value=0, new_value="430000"))
        args = conn.executed[0][1]
        self.assertEqual(args[2], "0")


class TestMvpSkipHumanReview(unittest.TestCase):
    """
    MVP_SKIP_HUMAN_REVIEW — временный переключатель: пока в Telegram нет
    команды подтверждения (только `python -m app.sync_job --approve ID`),
    needs_human=True держал бы каждую находку в очереди навсегда. Флаг
    выключен по умолчанию — эти тесты фиксируют оба состояния.
    """

    def setUp(self):
        self._orig_pool = db.pool
        self._orig_flag = db.MVP_SKIP_HUMAN_REVIEW

    def tearDown(self):
        db.pool = self._orig_pool
        db.MVP_SKIP_HUMAN_REVIEW = self._orig_flag

    def test_flag_off_by_default_keeps_needs_human_true(self):
        conn = _FakeExecuteConn()
        db.pool = _FakePool(conn)
        db.MVP_SKIP_HUMAN_REVIEW = False
        asyncio.run(db.save_extraction(
            message_id=1, chat_id=1, project_recid="Mangata",
            object_guess="Unit 1 (2 BR)", confidence=0.95, slot="unit_price",
            url_status="parsed", why="Price: 100000$", needs_human=True,
        ))
        _, args = conn.executed[0]
        self.assertTrue(args[8])

    def test_flag_on_forces_needs_human_false(self):
        conn = _FakeExecuteConn()
        db.pool = _FakePool(conn)
        db.MVP_SKIP_HUMAN_REVIEW = True
        with self.assertLogs('Database', level='INFO') as log:
            asyncio.run(db.save_extraction(
                message_id=1, chat_id=1, project_recid="Mangata",
                object_guess="Unit 1 (2 BR)", confidence=0.95, slot="unit_price",
                url_status="parsed", why="Price: 100000$", needs_human=True,
            ))
        _, args = conn.executed[0]
        self.assertFalse(args[8])
        self.assertTrue(any("MVP_SKIP_HUMAN_REVIEW" in m for m in log.output))

    def test_flag_on_does_not_flip_an_explicit_false(self):
        """Already-approved rows (needs_human=False) stay untouched either way."""
        conn = _FakeExecuteConn()
        db.pool = _FakePool(conn)
        db.MVP_SKIP_HUMAN_REVIEW = True
        asyncio.run(db.save_extraction(
            message_id=1, chat_id=1, project_recid="Mangata",
            object_guess="Unit 1 (2 BR)", confidence=0.95, slot="unit_price",
            url_status="parsed", why="Price: 100000$", needs_human=False,
        ))
        _, args = conn.executed[0]
        self.assertFalse(args[8])


class TestDatabaseLifecycle(unittest.IsolatedAsyncioTestCase):

    async def test_create_pool_retries_after_transient_failure(self):
        expected_pool = _FakePool(_FakeConn())

        class _FakeAsyncpg:
            def __init__(self):
                self.calls = 0

            async def create_pool(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("Postgres is still starting")
                return expected_pool

        fake_asyncpg = _FakeAsyncpg()
        with (
            patch.object(db, "asyncpg", fake_asyncpg),
            patch.object(db.asyncio, "sleep", AsyncMock()),
        ):
            pool = await db.create_pool_with_retry(
                "postgresql://test",
                attempts=2,
                retry_base_seconds=0,
                retry_max_seconds=0,
                connect_timeout_seconds=1,
            )

        self.assertIs(pool, expected_pool)
        self.assertEqual(fake_asyncpg.calls, 2)

    async def test_approval_releases_only_review_pending_row(self):
        original_pool = db.pool
        conn = _FakeExecuteConn("UPDATE 1")
        db.pool = _FakePool(conn)
        try:
            approved = await db.approve_pending_extraction(42)
        finally:
            db.pool = original_pool

        self.assertTrue(approved)
        query, args = conn.executed[0]
        self.assertIn("needs_human = FALSE", query)
        self.assertIn("sync_status = 'pending'", query)
        self.assertEqual(args, (42,))

    async def test_migrations_add_durable_sync_queue_fields(self):
        conn = _FakeExecuteConn()
        pool = _FakePool(conn)

        ready = await db.ensure_database_schema(pool)

        self.assertTrue(ready)
        sql = "\n".join(query for query, _args in conn.executed)
        self.assertIn("sync_claim_token", sql)
        self.assertIn("sync_lease_until", sql)
        self.assertIn("sync_next_retry_at", sql)
        self.assertIn("idx_extractions_sync_ready", sql)
        self.assertIn("ingest_key", sql)
        self.assertIn("uq_extractions_ingest_key", sql)


if __name__ == '__main__':
    unittest.main()
