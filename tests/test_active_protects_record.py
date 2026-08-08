"""
'Active' на проекте защищает его и его юниты от перезаписи.

До этого изменения (владелец, 06.08.2026) 'Active' был витриной без функции:
HUMAN_ONLY_FIELDS запрещал коду только САМ писать эту галочку — сама запись
проверку не читала вообще. Поставив Active на Rise Villas и запустив разбор
заново, пользователь получил бы то же обновление, как если бы галочки не
было: следующий прогон свободно менял поля проекта и его юнитов.

Правило сужено 08.08.2026 (владелец): Active защищает ЗАПОЛНЕННЫЕ значения, но
не запрещает дописывать недостающее. У существующего проекта с Active=True
пустые поля дозаполняются, заполненные не трогаются, а отсутствующие юниты
создаются как обычно; у существующего юнита действует то же поле-в-поле.

Прежний вариант отклонял запись целиком, и это обошлось дорого: у Axis One и
Y-WAY юнитов не существовало вовсе — защищать было нечего, — но создание всё
равно блокировалось, и проекты простояли пустыми, пока владелец не снял
галочки руками. Запрет на создание отсутствующего ничего не защищает.

Служебные поля (Status, Gaps, Last updated, Source) под Active не пишутся
никогда: они считаются из payload и затёрли бы человеческий вердикт.

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
    async def test_active_project_keeps_filled_values(self, fake_table, monkeypatch):
        """Заполненное не меняется: District у записи уже есть."""
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=True)])

        rec_id = await ac.upsert_project(
            {'Project Name': 'Rise Villas', 'District': 'Другой район'},
            dev_id='recDev', gaps=[],
        )

        assert rec_id == 'recActiveProj', "id найденной записи должен вернуться как есть"
        assert fake_table.created == []
        # Дозаполнение пустого допустимо (у фикстуры нет Developer), но
        # заполненный District обязан остаться нетронутым.
        for _rec_id, written in fake_table.updated:
            assert 'District' not in written, "заполненный District трогать нельзя"
            assert 'Project Name' not in written or written['Project Name'] == 'Rise Villas'

    @pytest.mark.asyncio
    async def test_active_project_still_fills_empty_fields(self, fake_table, monkeypatch):
        """Пустое дозаполняется: запрет на это ничего не защищал бы.

        Ради этого правило и сузили (владелец, 08.08.2026) — прежний вариант
        отклонял запись целиком, и Axis One с Y-WAY простояли пустыми.
        """
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=True)])

        rec_id = await ac.upsert_project(
            {'Project Name': 'Rise Villas', 'District': 'Другой район',
             'Price From (USD)': 999999},
            dev_id='recDev', gaps=[],
        )

        assert rec_id == 'recActiveProj'
        assert len(fake_table.updated) == 1, "пустая Price From (USD) должна дозаполниться"
        written = fake_table.updated[0][1]
        assert written['Price From (USD)'] == 999999
        assert 'District' not in written, "заполненный District трогать нельзя"
        for service_field in ('Status', 'Gaps', 'Last updated', 'Source'):
            assert service_field not in written, (
                f"{service_field} считается из payload и затёр бы человеческий вердикт"
            )

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
    async def test_missing_unit_of_active_project_is_created(self, fake_table, monkeypatch):
        """Отсутствующий юнит создаётся: пустого места защищать нечего.

        Именно этот запрет и стоил дороже всего — у Axis One и Y-WAY юнитов не
        существовало, а создание отклонялось (владелец, 08.08.2026).
        """
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=True)])

        rec_id = await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 2, 'Price from (USD)': 999999},
            'recActiveProj', 'Rise Villas', [],
        )

        assert rec_id not in (None, ac.SKIPPED_ACTIVE)
        assert len(fake_table.created) == 1
        assert fake_table.created[0]['Price from(USD)'] == 999999

    @pytest.mark.asyncio
    async def test_existing_unit_of_active_project_keeps_filled_values(self, fake_table, monkeypatch):
        """У существующего юнита заполненная цена не перезаписывается, пустая площадь — да."""
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [_active_project_record(active=True)])
        monkeypatch.setattr(ac, 'CACHE_UNITS', [{
            'id': 'recUnit1',
            'fields': {'Key': 'rise-villas__villa__2br', 'Price from(USD)': 290000},
        }])

        rec_id = await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 2, 'Price from (USD)': 999999,
             'Area from (m2)': 153.5},
            'recActiveProj', 'Rise Villas', [],
        )

        assert rec_id == 'recUnit1'
        assert fake_table.created == []
        assert len(fake_table.updated) == 1
        written = fake_table.updated[0][1]
        assert 'Price from(USD)' not in written, "заполненную цену трогать нельзя"
        assert written['Area from (m\xb2)'] == 153.5, "пустая площадь должна дозаполниться"

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
