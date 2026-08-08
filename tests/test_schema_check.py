import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import schema_check
from app.schema_check import check_schema_drift, expected_select_values


@pytest.fixture(autouse=True)
def _no_live_schema_calls(monkeypatch):
    """
    Проверка типов полей ходит в живую схему Airtable. В тестах глушим её,
    иначе прогон зависит от сети и от того, задан ли AIRTABLE_TOKEN.
    """
    monkeypatch.setattr(schema_check, 'get_field_type', lambda table, field: 'singleLineText')


class TestDriftDetection:
    """
    Проверяем, что детектор действительно ловит расхождения, а не всегда
    возвращает пустой список. Без этих тестов проверка схемы была бы
    ровно тем же «зеленым тестом, который ничего не проверяет».
    """

    def test_reports_missing_field(self, monkeypatch):
        monkeypatch.setattr(schema_check, 'field_exists',
                            lambda table, field: field != 'District')
        monkeypatch.setattr(schema_check, 'get_select_options',
                            lambda table, field, fallback=None: ['заглушка'])

        problems = check_schema_drift()
        assert any("District" in p and "поле отсутствует" in p for p in problems)

    def test_reports_select_value_absent_in_base(self, monkeypatch):
        monkeypatch.setattr(schema_check, 'field_exists', lambda table, field: True)
        # База знает только Medium, а код умеет отдавать еще Hight и Low
        monkeypatch.setattr(schema_check, 'get_select_options',
                            lambda table, field, fallback=None: ['Medium'])

        problems = check_schema_drift()
        joined = " ".join(problems)
        assert "Priority" in joined
        assert "Hight" in joined or "Low" in joined

    def test_reports_unreadable_select(self, monkeypatch):
        monkeypatch.setattr(schema_check, 'field_exists', lambda table, field: True)
        monkeypatch.setattr(schema_check, 'get_select_options',
                            lambda table, field, fallback=None: [])

        problems = check_schema_drift()
        assert any("селект не прочитан" in p for p in problems)

    def test_clean_when_everything_matches(self, monkeypatch):
        expected = expected_select_values()
        monkeypatch.setattr(schema_check, 'field_exists', lambda table, field: True)
        monkeypatch.setattr(
            schema_check, 'get_select_options',
            lambda table, field, fallback=None: sorted(expected.get((table, field), {'заглушка'}))
        )
        assert check_schema_drift() == []

    def test_reports_field_that_became_a_formula_in_the_live_base(self, monkeypatch):
        """
        Тот самый класс багов, ради которого проверка и писалась: поле стало
        формулой в интерфейсе Airtable, код продолжает в него писать, и вся
        запись падает на 422. Статический READ_ONLY_FIELDS такое не ловит —
        тип обязан читаться из живой схемы.
        """
        monkeypatch.setattr(schema_check, 'field_exists', lambda table, field: True)
        monkeypatch.setattr(schema_check, 'get_select_options', lambda table, field: ['заглушка'])
        monkeypatch.setattr(
            schema_check, 'get_field_type',
            lambda table, field: 'formula' if field == 'Bedrooms' else 'singleLineText',
        )

        problems = check_schema_drift()
        assert any("только для чтения" in p and "Bedrooms" in p for p in problems)

    def test_reports_readonly_formula_fields_in_required(self, monkeypatch):
        monkeypatch.setattr(schema_check, 'field_exists', lambda table, field: True)
        monkeypatch.setattr(schema_check, 'get_select_options', lambda table, field: ['заглушка'])
        # monkeypatch.setitem auto-reverts on teardown — no manual cleanup needed
        monkeypatch.setitem(schema_check.REQUIRED_FIELDS, 'Units', ['Unit ID'])

        problems = check_schema_drift()
        assert any("read-only" in p and "Unit ID" in p for p in problems)


class TestExpectationsComeFromCode:
    """
    Ожидаемые значения должны браться из рабочих структур кода, а не из
    отдельной копии списка — иначе мы просто заводим третий список, который
    тоже разойдется с базой.
    """

    def test_district_values_come_from_area_aliases(self):
        from app.airtable_client import AREA_ALIASES
        assert expected_select_values()[('Projects', 'District')] == set(AREA_ALIASES.values())

    def test_priority_values_come_from_mapping(self):
        from app.priority_parser import AIRTABLE_PRIORITY
        assert expected_select_values()[('Field Staging', 'Priority')] == set(AIRTABLE_PRIORITY.values())

    def test_land_zoning_values_come_from_aliases(self):
        """
        Регрессия 03.08.2026: 'Red/Commercial' не было в живой базе ни в
        промпте gemini_parser.py, ни где-либо ещё - никакой проверки для этого
        поля не существовало вовсе, хотя точно такой же инцидент ("промпт
        требовал 'Commercial', где база знает 'Brown'") уже случался раньше.
        """
        from app.airtable_client import LAND_ZONING_ALIASES
        assert (expected_select_values()[('Projects', 'Land Zoning Color')]
                == set(LAND_ZONING_ALIASES.values()))


@pytest.mark.skipif(not os.environ.get('AIRTABLE_TOKEN'),
                    reason="нет AIRTABLE_TOKEN — проверка живой базы пропущена")
class TestAgainstLiveBase:
    """Реальный round-trip к Airtable, а не проверка переменных в памяти."""

    def test_live_schema_has_no_drift(self):
        problems = check_schema_drift()
        assert problems == [], "Расхождение кода и живой базы:\n" + "\n".join(problems)

    def test_readonly_list_names_exist_and_really_are_computed(self):
        """
        Страховочный список бесполезен, если в нём опечатка: 'Price per m²' в
        базе не существует, настоящее имя — 'Price per m² from(USD)'. Такой
        элемент защищает от несуществующего поля и молча ничего не проверяет.
        """
        from app.airtable_client import get_field_type
        from app.schema_check import COMPUTED_FIELD_TYPES, READ_ONLY_FIELDS

        for table, names in READ_ONLY_FIELDS.items():
            for name in names:
                ftype = get_field_type(table, name)
                assert ftype is not None, f"{table}.{name!r} нет в базе — опечатка в списке"
                assert ftype in COMPUTED_FIELD_TYPES, (
                    f"{table}.{name!r} имеет тип {ftype!r} и не является вычисляемым"
                )
