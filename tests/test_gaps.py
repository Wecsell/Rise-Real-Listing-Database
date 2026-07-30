"""
Расчет Gaps.

История бага: field_processor передавал в upsert_project жестко зашитый
gaps=[], а airtable_client ставит Status = "Needs data" if gaps else "Verified"
и затирает поле Gaps пустой строкой. Из-за этого находка по фото баннера и
голосовухе — заведомо неполная — приезжала в базу как Verified без единого
отмеченного пропуска. Механизм, на котором держится дальнейший запрос
недостающих данных у застройщика, был обезврежен в единственном месте, где
он реально вызывался.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.gaps import (
    REQUIRED_PROJECT_FIELDS,
    compute_gaps,
    developer_gaps,
    is_filled,
    merge_gaps,
    project_gaps,
    unit_gaps,
)


class TestIsFilled:
    """Пустоту присылают в разных видах — и Airtable, и модель."""

    @pytest.mark.parametrize("value", [None, "", "   ", [], {}, "None", "n/a", "-", "нет данных", "UNKNOWN"])
    def test_empty_variants(self, value):
        assert is_filled(value) is False

    @pytest.mark.parametrize("value", ["Canggu", 0, 1, 250000, ["Villa"], {"a": 1}, False])
    def test_filled_variants(self, value):
        assert is_filled(value) is True

    def test_zero_counts_as_filled(self):
        """0 — законная цена/расстояние, а не пропуск."""
        assert is_filled(0) is True


class TestProjectGaps:

    def test_empty_project_reports_everything(self):
        gaps = project_gaps({})
        assert len(gaps) == len(REQUIRED_PROJECT_FIELDS)
        assert 'название проекта' in gaps
        assert 'шахматка' in gaps

    def test_full_project_has_no_gaps(self):
        fields = {
            'Project Name': 'Rise Villas Canggu',
            'District': 'Canggu',
            'Property Type': ['Villa'],
            'Price From (USD)': 250000,
            'Construction stage': 'Structure',
            'Handover Date': '2027-03-31',
            'Ownership Type': 'Freehold',
            'Land Zoning Color': 'Tourism/Mixed',
            'Handover Permits': 'PBG',
            'Link to Dev Kit (Rus)': 'https://example.com/kit.pdf',
            'Availability Chart': 'https://example.com/chart',
        }
        assert project_gaps(fields) == []

    def test_banner_only_finding_is_mostly_gaps(self):
        """Типичная находка из поля: есть название и район, больше ничего."""
        gaps = project_gaps({'Project Name': 'Some Villas', 'District': 'Canggu'})
        assert 'название проекта' not in gaps
        assert 'район' not in gaps
        assert 'цена от (USD)' in gaps
        assert 'разрешение на строительство (PBG/SLF)' in gaps
        assert len(gaps) >= 8


class TestConditionalLeaseTerm:
    """Срок аренды осмысленно спрашивать только у leasehold."""

    def test_leasehold_without_term_is_a_gap(self):
        gaps = project_gaps({'Ownership Type': 'Leasehold'})
        assert 'срок аренды (лет)' in gaps

    def test_leasehold_with_term_is_not_a_gap(self):
        gaps = project_gaps({'Ownership Type': 'Leasehold', 'Lease Term (years)': 30})
        assert 'срок аренды (лет)' not in gaps

    def test_freehold_never_asks_for_lease_term(self):
        gaps = project_gaps({'Ownership Type': 'Freehold'})
        assert 'срок аренды (лет)' not in gaps


class TestUnitAndDeveloperGaps:

    def test_unit_gaps(self):
        gaps = unit_gaps({'Unit type': 'Villa', 'Bedrooms': 2})
        assert 'тип юнита' not in gaps
        assert 'цена юнита (USD)' in gaps

    def test_developer_without_contacts(self):
        """Без контактов аутрич невозможен — это главный пропуск."""
        gaps = developer_gaps({'Developer': 'Rise Development'})
        assert gaps == ['контакты']


class TestMergeGaps:

    def test_dedupes_case_insensitively(self):
        assert merge_gaps(['Цена'], ['цена'], ['Район']) == ['Цена', 'Район']

    def test_accepts_comma_separated_string_from_airtable(self):
        """Airtable хранит Gaps одной строкой через запятую."""
        assert merge_gaps('цена, район', ['шахматка']) == ['цена', 'район', 'шахматка']

    def test_ignores_empty_sources(self):
        assert merge_gaps(None, [], '', ['цена']) == ['цена']

    def test_preserves_order(self):
        assert merge_gaps(['a', 'b'], ['c']) == ['a', 'b', 'c']


class TestStatusConsequence:
    """
    Смысл всей механики: непустой Gaps обязан приводить к Needs data.
    Проверяем ту же логику, что стоит в upsert_project.
    """

    def test_incomplete_finding_would_be_needs_data(self):
        gaps = project_gaps({'Project Name': 'Some Villas'})
        status = "Needs data" if gaps else "Verified"
        assert status == "Needs data"

    def test_complete_project_would_be_verified(self):
        fields = {k: 'заполнено' for k in REQUIRED_PROJECT_FIELDS}
        fields['Ownership Type'] = 'Freehold'
        gaps = project_gaps(fields)
        status = "Needs data" if gaps else "Verified"
        assert status == "Verified"
