import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import schema_check
from app.schema_check import check_schema_drift, expected_select_values


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


@pytest.mark.skipif(not os.environ.get('AIRTABLE_TOKEN'),
                    reason="нет AIRTABLE_TOKEN — проверка живой базы пропущена")
class TestAgainstLiveBase:
    """Реальный round-trip к Airtable, а не проверка переменных в памяти."""

    def test_live_schema_has_no_drift(self):
        problems = check_schema_drift()
        assert problems == [], "Расхождение кода и живой базы:\n" + "\n".join(problems)
