"""
'Active' на проекте защищает его и его юниты от перезаписи.

До этого изменения (владелец, 06.08.2026) 'Active' был витриной без функции:
HUMAN_ONLY_FIELDS запрещал коду только САМ писать эту галочку — сама запись
проверку не читала вообще. Поставив Active на Rise Villas и запустив разбор
заново, пользователь получил бы то же обновление, как если бы галочки не
было: следующий прогон свободно менял поля проекта и его юнитов.

Теперь: если у ЗАЙДЕННОГО (существующего) проекта Active=True, upsert_project
не пишет вообще ничего и возвращает id найденной записи без изменений;
upsert_unit для юнитов этого проекта тоже не пишет ничего — защита работает
независимо от того, кто вызывает запись юнита.

Новый проект (Active нет, потому что записи ещё не существует) создаётся как
обычно — иначе первый разбор был бы невозможен.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.airtable_client as ac


class _FakeTable:
    _id_counter = 0

    def __init__(self):
        self.created = []
        self.updated = []

    def create(self, fields=None, **kwargs):
        payload = fields if fields is not None else kwargs.get('fields', {})
        self.created.append(payload)
        _FakeTable._id_counter += 1
        return {'id': f'recNew{_FakeTable._id_counter}', 'fields': payload}

    def update(self, rec_id, fields=None, **kwargs):
        self.updated.append((rec_id, fields or {}))
        return {'id': rec_id, 'fields': fields or {}}


@pytest.fixture
def fake_table(monkeypatch):
    table = _FakeTable()
    monkeypatch.setattr(ac, 'get_table', lambda _name: table)
    monkeypatch.setattr(ac, 'cache_is_stale', lambda: False)
    monkeypatch.setattr(ac, 'CACHE_UNITS', [])
    monkeypatch.setattr(ac, 'CACHE_UNITS_SECONDARY', [])
    return table


def _active_project_record(active=True):
    return {
        'id': 'recActiveProj',
        'fields': {
            'Project Name': 'Rise Villas',
            'District': 'Nuanu',
            'Active': active,
        },
    }


class TestUpsertProjectRespectsActive:

    @pytest.mark.asyncio
    async def test_active_project_is_not_updated(self, fake_table, monkeypatch):
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=True)])

        rec_id = await ac.upsert_project(
            {'Project Name': 'Rise Villas', 'District': 'Nuanu', 'Price From (USD)': 999999},
            dev_id='recDev', gaps=[],
        )

        assert rec_id == 'recActiveProj', "id найденной записи должен вернуться как есть"
        assert fake_table.updated == [], "Active-проект не должен уходить в table.update"
        assert fake_table.created == []

    @pytest.mark.asyncio
    async def test_inactive_project_is_updated_as_before(self, fake_table, monkeypatch):
        """Регресс: защита не должна включаться, когда Active не стоит."""
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=False)])

        rec_id = await ac.upsert_project(
            {'Project Name': 'Rise Villas', 'District': 'Nuanu', 'Price From (USD)': 300000},
            dev_id='recDev', gaps=[],
        )

        assert rec_id == 'recActiveProj'
        assert len(fake_table.updated) == 1, "без Active обновление должно пройти как обычно"

    @pytest.mark.asyncio
    async def test_new_project_is_created_even_though_no_active_flag_exists_yet(self, fake_table, monkeypatch):
        """Записи ещё нет — защищать нечего, разбор не должен блокироваться навсегда."""
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [])

        rec_id = await ac.upsert_project(
            {'Project Name': 'Brand New Project', 'District': 'Nuanu', 'Price From (USD)': 200000},
            dev_id='recDev', gaps=[],
        )

        assert rec_id is not None
        assert len(fake_table.created) == 1


class TestUpsertUnitRespectsProjectActive:

    @pytest.mark.asyncio
    async def test_unit_of_active_project_is_not_written(self, fake_table, monkeypatch):
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=True)])

        rec_id = await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 2, 'Price from (USD)': 999999},
            'recActiveProj', 'Rise Villas', [],
        )

        # SKIPPED_ACTIVE, а не None: сентинел истинный и отличим от сбоя
        # записи (см. tests/test_active_extended_guards.py — иначе
        # field_processor.py и app/sync_job.py читают пропуск как провал).
        assert rec_id is ac.SKIPPED_ACTIVE
        assert fake_table.created == []
        assert fake_table.updated == []

    @pytest.mark.asyncio
    async def test_unit_of_inactive_project_is_written_as_before(self, fake_table, monkeypatch):
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=False)])

        rec_id = await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 2, 'Price from (USD)': 300000},
            'recActiveProj', 'Rise Villas', [],
        )

        assert rec_id is not None
        assert len(fake_table.created) == 1

    @pytest.mark.asyncio
    async def test_unit_of_unknown_project_id_is_written_as_before(self, fake_table, monkeypatch):
        """proj_id не найден в кэше (например, кэш ещё не прогрелся) — не блокируем вслепую."""
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [])

        rec_id = await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 2, 'Price from (USD)': 300000},
            'recSomeProj', 'Some Project', [],
        )

        assert rec_id is not None
        assert len(fake_table.created) == 1
