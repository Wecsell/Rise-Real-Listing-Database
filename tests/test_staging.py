"""
Решения о переносе находки из Field Staging в основную базу.

Два бага, которые эти правила закрывают:

1. Автоперенос. field_processor содержал захардкоженное ans = 'y' и писал в
   Developer/Projects/Units сразу после разбора. Находка по фото баннера —
   это гипотеза, и она попадала в базу как факт.

2. Дубли. Записи создавались в основной базе, и только потом находка
   помечалась Processed. Падение между шагами означало повторное создание
   на следующем тике через 30 секунд.
"""
import json
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.staging import (
    already_promoted,
    is_confirmed,
    load_parsed,
    needs_parsing,
    promotion_blockers,
    should_promote,
)

PARSED = json.dumps({'Developer': {'Developer': 'Rise'}, 'Projects': {'Project Name': 'Villas'}})


def record(**fields):
    return {'id': 'rec123', 'fields': fields}


class TestParsingStage:

    def test_new_record_needs_parsing(self):
        assert needs_parsing(record(Status='New')) is True

    @pytest.mark.parametrize("status", ['Processed', 'Error'])
    def test_other_statuses_do_not(self, status):
        assert needs_parsing(record(Status=status)) is False

    def test_missing_status_does_not(self):
        assert needs_parsing(record()) is False


class TestConfirmationGate:
    """Без галочки Confirmed находка в основную базу не едет."""

    def test_unconfirmed_is_not_promoted(self):
        assert should_promote(record(**{'Parsed JSON': PARSED})) is False

    def test_confirmed_with_parsed_json_is_promoted(self):
        rec = record(**{'Confirmed': True, 'Parsed JSON': PARSED})
        assert should_promote(rec) is True

    def test_confirmed_without_parsed_json_is_not_promoted(self):
        assert should_promote(record(Confirmed=True)) is False

    def test_confirmed_false_is_not_promoted(self):
        rec = record(**{'Confirmed': False, 'Parsed JSON': PARSED})
        assert should_promote(rec) is False

    def test_is_confirmed_reads_checkbox(self):
        assert is_confirmed(record(Confirmed=True)) is True
        assert is_confirmed(record()) is False


class TestIdempotency:
    """Заполненная связь Project означает, что перенос уже был."""

    def test_record_with_project_link_is_already_promoted(self):
        assert already_promoted(record(Project=['recProj1'])) is True

    def test_record_without_project_link_is_not(self):
        assert already_promoted(record()) is False
        assert already_promoted(record(Project=[])) is False

    def test_promoted_record_is_not_promoted_again(self):
        """Ключевая проверка: повторный проход не создаст второй проект."""
        rec = record(**{'Confirmed': True, 'Parsed JSON': PARSED, 'Project': ['recProj1']})
        assert should_promote(rec) is False

    def test_crash_before_linking_allows_retry(self):
        """
        Падение до простановки связи оставляет запись доступной для повтора —
        это нормально: upsert найдет созданный проект и обновит его.
        """
        rec = record(**{'Confirmed': True, 'Parsed JSON': PARSED})
        assert should_promote(rec) is True


class TestParsedJson:

    def test_valid_json_is_loaded(self):
        data = load_parsed(record(**{'Parsed JSON': PARSED}))
        assert data['Developer']['Developer'] == 'Rise'

    def test_broken_json_returns_none_instead_of_raising(self):
        assert load_parsed(record(**{'Parsed JSON': '{не json'})) is None

    def test_missing_field_returns_none(self):
        assert load_parsed(record()) is None

    def test_json_array_is_rejected(self):
        """Модель иногда отдает список — переносить такое нельзя."""
        assert load_parsed(record(**{'Parsed JSON': '[{"a": 1}]'})) is None


class TestBlockerReporting:

    def test_lists_every_reason(self):
        reasons = promotion_blockers(record())
        assert 'нет отметки Confirmed' in reasons
        assert 'нет разобранного JSON' in reasons

    def test_no_reasons_when_ready(self):
        rec = record(**{'Confirmed': True, 'Parsed JSON': PARSED})
        assert promotion_blockers(rec) == []
