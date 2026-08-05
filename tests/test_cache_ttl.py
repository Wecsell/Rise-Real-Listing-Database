"""
Срок годности кэша Airtable.

История бага: кэш читался один раз за процесс и больше не обновлялся никогда.
listener живет неделями (restart: always), а field_processor и sync_job — это
отдельные процессы со своими копиями кэша. Запись, созданную другим процессом
или добавленную человеком руками, поиск дубля не находил и создавал дубль.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import airtable_client as ac


class FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def all(self, fields=None):
        return self.rows


class FakeBase:
    def __init__(self):
        self.reads = 0

    def table(self, name):
        self.reads += 1
        return FakeTable([{'id': f'rec_{name}', 'fields': {}}])


@pytest.fixture
def fresh_cache(monkeypatch):
    """Изолируем глобальное состояние кэша между тестами."""
    monkeypatch.setattr(ac, 'CACHE_INITIALIZED', False)
    monkeypatch.setattr(ac, 'CACHE_LOADED_AT', 0.0)
    monkeypatch.setattr(ac, 'CACHE_DEVELOPERS', [])
    monkeypatch.setattr(ac, 'CACHE_PROJECTS', [])
    monkeypatch.setattr(ac, 'CACHE_UNITS', [])
    base = FakeBase()
    monkeypatch.setattr(ac, 'get_table', lambda name: base.table(name))
    return base


class TestStaleness:

    def test_stale_before_first_load(self, fresh_cache):
        assert ac.cache_is_stale() is True

    def test_fresh_right_after_load(self, fresh_cache):
        ac.init_cache()
        assert ac.cache_is_stale() is False

    def test_stale_after_ttl_elapsed(self, fresh_cache, monkeypatch):
        ac.init_cache()
        assert ac.cache_is_stale() is False

        # Перематываем время за пределы TTL
        loaded_at = ac.CACHE_LOADED_AT
        monkeypatch.setattr(ac.time, 'time',
                            lambda: loaded_at + ac.CACHE_TTL_SECONDS + 1)
        assert ac.cache_is_stale() is True

    def test_still_fresh_just_inside_ttl(self, fresh_cache, monkeypatch):
        ac.init_cache()
        loaded_at = ac.CACHE_LOADED_AT
        monkeypatch.setattr(ac.time, 'time',
                            lambda: loaded_at + ac.CACHE_TTL_SECONDS - 1)
        assert ac.cache_is_stale() is False

    def test_force_reload_ignores_freshness(self, fresh_cache):
        ac.init_cache()
        assert ac.cache_is_stale() is False
        ac.init_cache(force=True)
        assert ac.cache_is_stale() is False


class TestRefetchBehaviour:
    """Кэш не должен перечитывать базу, пока он свежий."""

    def test_second_call_does_not_hit_airtable(self, fresh_cache):
        ac.init_cache()
        reads_after_first = fresh_cache.reads
        assert reads_after_first == 3  # Developer, Projects, Units

        ac.init_cache()
        assert fresh_cache.reads == reads_after_first, \
            "Свежий кэш не должен ходить в Airtable повторно"

    def test_refetches_once_stale(self, fresh_cache, monkeypatch):
        ac.init_cache()
        loaded_at = ac.CACHE_LOADED_AT
        monkeypatch.setattr(ac.time, 'time',
                            lambda: loaded_at + ac.CACHE_TTL_SECONDS + 1)

        ac.init_cache()
        assert fresh_cache.reads == 6, "Просроченный кэш обязан перечитать базу"

    def test_force_refetches_even_when_fresh(self, fresh_cache):
        ac.init_cache()
        ac.init_cache(force=True)
        assert fresh_cache.reads == 6


class TestAsyncVariant:

    @pytest.mark.asyncio
    async def test_async_refreshes_when_stale(self, fresh_cache):
        await ac.init_cache_async()
        assert fresh_cache.reads == 3
        assert ac.cache_is_stale() is False

    @pytest.mark.asyncio
    async def test_async_skips_when_fresh(self, fresh_cache):
        await ac.init_cache_async()
        await ac.init_cache_async()
        assert fresh_cache.reads == 3
