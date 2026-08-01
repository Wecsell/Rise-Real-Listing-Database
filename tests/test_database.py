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


if __name__ == '__main__':
    unittest.main()
