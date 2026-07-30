"""
Нормализация значений перед записью в Airtable и нечёткий поиск дублей.

Эти функции — ядро борьбы с дублями, ради которой в проекте написаны
check_duplicates.py и merge_52_53.py, но покрытие app/airtable_client.py
составляло 22%: сама логика сопоставления не проверялась ничем.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.airtable_client import (
    format_drive_link,
    fuzzy_match_developer,
    fuzzy_match_project,
    safe_float,
    sanitize_pool,
    sanitize_unit_type,
)

MATCH_THRESHOLD = 0.90


def project(name, rec_id='rec1', district=None, developer=None):
    fields = {'Project Name': name}
    if district:
        fields['District'] = district
    if developer:
        fields['Developer'] = developer
    return {'id': rec_id, 'fields': fields}


def developer(name, rec_id='recDev1'):
    return {'id': rec_id, 'fields': {'Developer': name}}


class TestProjectMatching:

    def test_exact_name_matches(self):
        existing = [project('Rise Villas Canggu')]
        match, score = fuzzy_match_project('Rise Villas Canggu', existing)
        assert match is existing[0]
        assert score >= MATCH_THRESHOLD

    def test_typo_still_matches(self):
        existing = [project('Rise Villas Canggu')]
        match, _ = fuzzy_match_project('Rise Vilas Canggu', existing)
        assert match is existing[0]

    def test_unrelated_project_does_not_match(self):
        existing = [project('Rise Villas Canggu')]
        match, score = fuzzy_match_project('Ocean Breeze Uluwatu', existing)
        assert match is None
        assert score == 0.0

    def test_empty_inputs_are_safe(self):
        assert fuzzy_match_project(None, [project('X')]) == (None, 0.0)
        assert fuzzy_match_project('X', []) == (None, 0.0)
        assert fuzzy_match_project('None', [project('X')]) == (None, 0.0)

    def test_records_without_name_are_skipped(self):
        existing = [{'id': 'rec1', 'fields': {}}, project('Rise Villas', 'rec2')]
        match, _ = fuzzy_match_project('Rise Villas', existing)
        assert match['id'] == 'rec2'


class TestDeveloperHierarchy:
    """
    Проект другого застройщика не должен считаться тем же проектом даже при
    совпадении названия: у разных девелоперов бывают одинаковые имена.
    """

    def test_project_of_another_developer_is_skipped(self):
        existing = [project('Rise Villas', developer=['recDevA'])]
        match, _ = fuzzy_match_project('Rise Villas', existing, dev_id='recDevB')
        assert match is None

    def test_project_of_same_developer_matches(self):
        existing = [project('Rise Villas', developer=['recDevA'])]
        match, _ = fuzzy_match_project('Rise Villas', existing, dev_id='recDevA')
        assert match is existing[0]

    def test_project_without_developer_is_still_considered(self):
        existing = [project('Rise Villas')]
        match, _ = fuzzy_match_project('Rise Villas', existing, dev_id='recDevA')
        assert match is existing[0]


class TestDistrictPenalty:
    """Совпадение имени в другом районе — скорее всего разные объекты."""

    def test_same_district_matches(self):
        existing = [project('Rise Villas', district='Canggu')]
        match, _ = fuzzy_match_project('Rise Villas', existing, area='Canggu')
        assert match is existing[0]

    def test_district_mismatch_is_penalised(self):
        existing = [project('Rise Villas', district='Canggu')]
        match, score = fuzzy_match_project('Rise Villas', existing, area='Ubud')
        assert match is None, f"штраф за район не применился, score={score}"


class TestDeveloperMatching:

    def test_exact_match(self):
        existing = [developer('Rise Development')]
        match, score = fuzzy_match_developer('Rise Development', existing)
        assert match is existing[0]
        assert score >= MATCH_THRESHOLD

    def test_different_developer_does_not_match(self):
        existing = [developer('Rise Development')]
        match, _ = fuzzy_match_developer('Nuanu Group', existing)
        assert match is None

    def test_empty_inputs_are_safe(self):
        assert fuzzy_match_developer(None, [developer('X')]) == (None, 0.0)
        assert fuzzy_match_developer('X', []) == (None, 0.0)


class TestSubsetBranchIsBelowThreshold:
    """
    ЗАФИКСИРОВАНО ТЕКУЩЕЕ ПОВЕДЕНИЕ, А НЕ ЖЕЛАЕМОЕ.

    В обеих функциях есть ветка «осмысленные слова записи входят в искомое
    имя»: она выставляет 0.85 для застройщика и 0.8 для проекта. Порог
    срабатывания — 0.90. То есть сама по себе эта ветка решение принять не
    может: она отрабатывает и не проходит гейт.

    Это ровно тот случай, ради которого ветка писалась — сопоставить
    'Rise Development' из базы с названием чата 'Rise Development Official
    Bali'. Сейчас такое сопоставление не происходит.

    Менять порог — решение бизнес-логики: ослабление грозит ошибочными
    слияниями разных проектов, ужесточение — дублями. Тесты фиксируют факт,
    чтобы изменение было осознанным.
    """

    def test_developer_name_inside_chat_title_does_not_match_today(self):
        existing = [developer('Rise Development Official Bali')]
        match, score = fuzzy_match_developer('Rise Development', existing)
        assert match is None
        assert score == 0.0

    def test_project_phase_does_not_match_today(self):
        existing = [project('Rise Villas')]
        match, score = fuzzy_match_project('Rise Villas Phase 2', existing)
        assert match is None
        assert score == 0.0


class TestUnitType:
    """
    Раньше функция могла вернуть 'Hotel' и 'Hotel room', которых в селекте
    Units.Unit type нет — Airtable отвергал такую запись целиком.
    """

    @pytest.mark.parametrize("raw,expected", [
        ('Villa', 'Villa'),
        ('villa', 'Villa'),
        ('  Studio  ', 'Studio'),
        ('2BR villa with pool', 'Villa'),
        ('mini villa', 'Villa'),
        ('bungalow', 'Villa'),
        ('residence', 'Villa'),
        ('villa 2br', 'Villa'),
    ])
    def test_known_types_normalise(self, raw, expected):
        assert sanitize_unit_type(raw) == expected

    def test_penthouse_keeps_its_own_type(self):
        """В базе есть отдельное значение Penthouse — сводить его к Apartment лишнее."""
        assert sanitize_unit_type('penthouse') == 'Penthouse'

    @pytest.mark.parametrize("raw", ['Hotel', 'Hotel room'])
    def test_types_absent_from_base_are_dropped(self, raw):
        assert sanitize_unit_type(raw) is None

    @pytest.mark.parametrize("raw", [None, '', 'commercial', 'нечто непонятное'])
    def test_unknown_is_dropped(self, raw):
        assert sanitize_unit_type(raw) is None

    def test_never_returns_junk_options(self):
        """'-' и \"Developer's stock\" есть в базе, но проставлять их нельзя."""
        for raw in ['-', "Developer's stock", 'developer stock']:
            assert sanitize_unit_type(raw) not in ('-', "Developer's stock")


class TestPool:

    @pytest.mark.parametrize("raw,expected", [
        ('No', 'No'),
        ('no', 'No'),
        ('Yes(Private)', 'Yes(Private)'),
        ('Yes (Private)', 'Yes(Private)'),
        ('private', 'Yes(Private)'),
        ('Yes(Shared)', 'Yes(Shared)'),
        ('Yes (Shared)', 'Yes(Shared)'),
        ('shared', 'Yes(Shared)'),
        ('yes', 'Yes(Private)'),
    ])
    def test_normalises_to_canonical_form(self, raw, expected):
        assert sanitize_pool(raw) == expected

    @pytest.mark.parametrize("raw", [None, '', 'maybe'])
    def test_unknown_is_dropped(self, raw):
        assert sanitize_pool(raw) is None


class TestDriveLink:
    """Канон из правил проекта: thumbnail-ссылка с sz=w2000."""

    @pytest.mark.parametrize("raw", [
        'https://drive.google.com/file/d/1ABC_xyz-123/view?usp=sharing',
        'https://drive.google.com/uc?export=download&id=1ABC_xyz-123',
    ])
    def test_converts_to_canonical_thumbnail(self, raw):
        assert format_drive_link(raw) == 'https://drive.google.com/thumbnail?id=1ABC_xyz-123&sz=w2000'

    def test_leaves_foreign_urls_alone(self):
        assert format_drive_link('https://example.com/photo.jpg') == 'https://example.com/photo.jpg'

    def test_handles_non_strings(self):
        assert format_drive_link(None) is None


class TestSafeFloat:
    """Модель регулярно пишет цену как '250,000' или '$250 000'."""

    @pytest.mark.parametrize("raw,expected", [
        ('250000', 250000.0),
        ('250,000', 250000.0),
        ('250 000', 250000.0),
        ('$250,000', 250000.0),
        (250000, 250000.0),
    ])
    def test_parses_messy_numbers(self, raw, expected):
        assert safe_float(raw) == expected

    def test_none_stays_none(self):
        assert safe_float(None) is None

    def test_unparseable_returns_input_unchanged(self):
        assert safe_float('по запросу') == 'по запросу'
