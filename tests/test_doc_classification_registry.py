"""
Тесты реестра классификаций (app/doc_classification_registry.py,
implementation_plan.md, Э2 - "Не сделано в Э2" из walkthrough 01.08.2026).

Паттерн фейкового пула - тот же, что tests/test_database.py: реестр обязан не
падать без БД (pool is None), а не только работать на живом Postgres.
"""
import asyncio
import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.database as database
import app.doc_classification_registry as registry


class _FakeConn:
    def __init__(self, fetchval_return=None):
        self._fetchval_return = fetchval_return
        self.executed = []

    async def fetchval(self, query, *args):
        return self._fetchval_return

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class _RaisingConn:
    """Симулирует обрыв соединения с БД - реестр обязан проглотить ошибку."""

    async def fetchval(self, query, *args):
        raise RuntimeError("connection lost")

    async def execute(self, query, *args):
        raise RuntimeError("connection lost")


class TestGetCachedClassification(unittest.TestCase):

    def setUp(self):
        self._orig_pool = database.pool

    def tearDown(self):
        database.pool = self._orig_pool

    def test_no_pool_returns_none(self):
        """
        Реестр - ускорение, не обязательное условие: без БД пайплайн обязан
        продолжать работу через дорогой fallback, а не падать.
        """
        database.pool = None
        result = asyncio.run(registry.get_cached_classification("file123"))
        self.assertIsNone(result)

    def test_no_file_id_returns_none_without_querying(self):
        database.pool = _FakePool(_FakeConn(fetchval_return="permits"))
        result = asyncio.run(registry.get_cached_classification(""))
        self.assertIsNone(result)

    def test_known_file_returns_cached_type(self):
        database.pool = _FakePool(_FakeConn(fetchval_return="zoning"))
        result = asyncio.run(registry.get_cached_classification("file123"))
        self.assertEqual(result, "zoning")

    def test_unknown_file_returns_none(self):
        database.pool = _FakePool(_FakeConn(fetchval_return=None))
        result = asyncio.run(registry.get_cached_classification("file123"))
        self.assertIsNone(result)

    def test_db_error_is_swallowed_not_raised(self):
        database.pool = _FakePool(_RaisingConn())
        result = asyncio.run(registry.get_cached_classification("file123"))
        self.assertIsNone(result)

    def test_reads_live_pool_assigned_after_import(self):
        """
        Регрессия: 'from app.database import pool' скопировал бы ссылку на
        момент импорта и навсегда остался бы None, потому что init_db()
        присваивает настоящий пул уже после того, как модули импортированы.
        Модуль обязан обращаться к database.pool на момент вызова.
        """
        database.pool = None
        self.assertIsNone(asyncio.run(registry.get_cached_classification("file123")))
        database.pool = _FakePool(_FakeConn(fetchval_return="permits"))
        self.assertEqual(asyncio.run(registry.get_cached_classification("file123")), "permits")


class TestSaveClassification(unittest.TestCase):

    def setUp(self):
        self._orig_pool = database.pool

    def tearDown(self):
        database.pool = self._orig_pool

    def test_no_pool_does_not_raise(self):
        database.pool = None
        asyncio.run(registry.save_classification("file123", "permits", "model_fallback", "gemini-3.5-flash"))

    def test_no_file_id_or_doc_type_skips_write(self):
        conn = _FakeConn()
        database.pool = _FakePool(conn)
        asyncio.run(registry.save_classification("", "permits", "model_fallback"))
        asyncio.run(registry.save_classification("file123", "", "model_fallback"))
        self.assertEqual(conn.executed, [], "пустой file_id/doc_type не должен писаться в базу")

    def test_valid_write_executes_insert(self):
        conn = _FakeConn()
        database.pool = _FakePool(conn)
        asyncio.run(registry.save_classification(
            "file123", "permits", "model_fallback", "gemini-3.5-flash",
        ))
        self.assertEqual(len(conn.executed), 1)
        query, args = conn.executed[0]
        self.assertIn("document_classifications", query)
        self.assertIn("ON CONFLICT", query)
        self.assertEqual(args, ("file123", "permits", "model_fallback", "gemini-3.5-flash"))

    def test_db_error_is_swallowed_not_raised(self):
        database.pool = _FakePool(_RaisingConn())
        asyncio.run(registry.save_classification("file123", "permits", "model_fallback"))


if __name__ == '__main__':
    unittest.main()
