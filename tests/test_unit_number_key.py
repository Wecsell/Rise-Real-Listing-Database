"""
Поюнитные записи по номеру юнита из шахматки застройщика.

Найдено 02.08.2026 при листинге Bali Baza. upsert_unit() умеет строить
канонический ключ `project__unitno__Nbr` из поля 'Unit Number', но это поле
не передавал НИКТО в проекте - ветка была мертва с момента написания.

Два следствия, оба тихие:

1. Без номера ключ вырождается в `project__type__Nbr`, то есть все юниты
   одного типа с одинаковым числом спален схлопываются в ОДНУ запись.
   На шахматке Baza Kedungu это 58 реальных юнитов -> ~4 записи: 32 студии
   1BR перезаписывают друг друга, остаётся цена и статус последней.

2. 'Unit Number' не удаляется из fields перед записью, а в схеме Units такого
   поля нет (там 'Unit ID'). Запись ушла бы в Airtable с несуществующим полем
   и упала на 422 - причём молча: robust_airtable_op гасит HTTPError и
   возвращает {'id': None}, то есть юнит просто не создался бы.
"""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.airtable_client as ac


class _FakeTable:
    def __init__(self):
        self.created = []

    def create(self, fields=None, **kwargs):
        payload = fields if fields is not None else kwargs.get('fields', {})
        self.created.append(payload)
        return {'id': f'recNew{len(self.created)}', 'fields': payload}

    def update(self, rec_id, fields=None, **kwargs):
        return {'id': rec_id, 'fields': fields or {}}


class _FakeBase:
    def __init__(self, table):
        self._table = table

    def table(self, name):
        return self._table


@pytest.fixture
def fake_table(monkeypatch):
    table = _FakeTable()
    monkeypatch.setattr(ac, 'get_table', lambda _name: table)
    monkeypatch.setattr(ac, 'CACHE_UNITS', [])
    monkeypatch.setattr(ac, 'cache_is_stale', lambda: False)
    return table


def _unit(number, unit_type, price, bedrooms=1):
    return {
        'Unit Number': number,
        'Unit type': unit_type,
        'Bedrooms': bedrooms,
        'Price from (USD)': price,
    }


class TestUnitNumberProducesDistinctRecords:

    @pytest.mark.asyncio
    async def test_same_type_and_bedrooms_stay_separate_records(self, fake_table):
        """
        Регрессия на схлопывание: две студии 1BR с РАЗНЫМИ номерами - это два
        разных юнита с разной ценой и статусом, а не один переписанный.
        """
        await ac.upsert_unit(_unit('SMT201', 'Studio', 89000), 'recProj', 'Baza Kedungu', [])
        await ac.upsert_unit(_unit('SMT204', 'Studio', 92500), 'recProj', 'Baza Kedungu', [])

        keys = [f['Key'] for f in fake_table.created]
        assert len(fake_table.created) == 2, "оба юнита должны создаться"
        assert keys[0] != keys[1], f"ключи схлопнулись в один: {keys}"
        assert 'smt201' in keys[0]
        assert 'smt204' in keys[1]

    @pytest.mark.asyncio
    async def test_unit_number_is_not_sent_as_airtable_field(self, fake_table):
        """
        'Unit Number' - служебный ключ для канонического Key, в схеме Units
        такого поля нет. Отправка его в Airtable роняет запись на 422, а
        robust_airtable_op гасит ошибку - юнит молча не создастся.
        """
        await ac.upsert_unit(_unit('AL101', 'Apartment', 115900), 'recProj', 'Baza Kedungu', [])

        written = fake_table.created[0]
        assert 'Unit Number' not in written, (
            f"'Unit Number' ушло в Airtable как поле: {sorted(written)}"
        )

    @pytest.mark.asyncio
    async def test_unit_id_is_never_written_because_it_is_computed(self, fake_table):
        """
        'Unit ID' в схеме есть, но это formula-поле: Airtable отвечает "cannot
        accept a value because the field is computed" и отвергает ВСЮ запись.
        Первая версия этого фикса переносила номер туда, и все 9 типологий
        Baza Kedungu молча не создались (живой прогон 02.08.2026).

        Регрессия на вывод: наличие поля в схеме не означает, что в него
        можно писать.
        """
        await ac.upsert_unit(_unit('AL101', 'Apartment', 115900), 'recProj', 'Baza Kedungu', [])

        written = fake_table.created[0]
        assert 'Unit ID' not in written, (
            f"'Unit ID' - вычисляемое поле, запись в него роняет юнит: {sorted(written)}"
        )

    @pytest.mark.asyncio
    async def test_without_unit_number_behaviour_is_unchanged(self, fake_table):
        """Старый путь (номера нет) остаётся прежним - ключ по типу и спальням."""
        await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 3, 'Price from (USD)': 537000},
            'recProj', 'Origins', [],
        )
        key = fake_table.created[0]['Key']
        assert key == 'origins__villa__3br'
        assert 'Unit ID' not in fake_table.created[0]

    @pytest.mark.asyncio
    async def test_studio_key_uses_studio_token_not_bedroom_count(self, fake_table):
        """
        Регрессия на вопрос владельца 05.08.2026: у Studio Bedrooms по
        определению не заполняется, поэтому запасное значение '0' в
        upsert_unit() уходило в ключ как '0br' - неотличимо от юнита
        другого типа, у которого число спален просто не извлеклось.
        """
        await ac.upsert_unit(
            {'Unit type': 'Studio', 'Price from (USD)': 89000},
            'recProj', 'Gapura', [],
        )
        key = fake_table.created[0]['Key']
        assert key == 'gapura__studio__studio'
        assert '0br' not in key

    @pytest.mark.asyncio
    async def test_studio_key_with_unit_number_also_uses_studio_token(self, fake_table):
        await ac.upsert_unit(
            _unit('B39', 'Studio', 89000, bedrooms=None),
            'recProj', 'Coco Hills', [],
        )
        key = fake_table.created[0]['Key']
        assert key == 'coco-hills__b39__studio'

    @pytest.mark.asyncio
    async def test_non_studio_unfilled_bedrooms_uses_nbr_token(self, fake_table):
        """
        Регрессия на вопрос владельца 05.08.2026: 0 спален не бывает - это
        всегда "не извлекли", а не реальная величина, поэтому не-Studio юнит
        без Bedrooms получает явный 'nbr', а не выдаваемый за настоящий '0br'.
        """
        await ac.upsert_unit(
            {'Unit type': 'Villa', 'Price from (USD)': 537000},
            'recProj', 'Origins', [],
        )
        key = fake_table.created[0]['Key']
        assert key == 'origins__villa__nbr'

    @pytest.mark.asyncio
    async def test_non_studio_explicit_zero_bedrooms_also_uses_nbr_token(self, fake_table):
        await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 0, 'Price from (USD)': 537000},
            'recProj', 'Origins', [],
        )
        key = fake_table.created[0]['Key']
        assert key == 'origins__villa__nbr'

    @pytest.mark.asyncio
    async def test_non_studio_real_bedroom_count_is_unaffected(self, fake_table):
        await ac.upsert_unit(
            {'Unit type': 'Villa', 'Bedrooms': 3, 'Price from (USD)': 537000},
            'recProj', 'Origins', [],
        )
        key = fake_table.created[0]['Key']
        assert key == 'origins__villa__3br'

    @pytest.mark.asyncio
    async def test_failed_existing_unit_update_returns_no_record_id(self, fake_table):
        """A failed update must reach the sync queue as a retryable failure."""
        ac.CACHE_UNITS = [
            {'id': 'recExisting', 'fields': {'Key': 'baza-kedungu__smt201__1br'}}
        ]
        failed_update = AsyncMock(return_value={'id': None})

        with patch.object(ac, 'robust_airtable_op_async', failed_update):
            result = await ac.upsert_unit(
                _unit('SMT201', 'Studio', 89000), 'recProj', 'Baza Kedungu', []
            )

        assert result is None
        failed_update.assert_awaited_once()
