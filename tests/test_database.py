import asyncio
import os
import sys
import unittest

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
    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))


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


if __name__ == '__main__':
    unittest.main()
