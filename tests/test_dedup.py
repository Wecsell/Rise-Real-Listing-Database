"""
Поиск повторных находок по телефону с баннера.

Правило владельца: телефон совпал — дубль, даже если название проекта другое.
Листинг устроен так, что после получения контакта у застройщика запрашиваются
все его проекты сразу, поэтому второй баннер с тем же номером новой работы
не создает.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.dedup import (
    POSSIBLE_AGENT,
    SAME_LEAD,
    build_duplicate_notice,
    classify_phone_match,
    describe_duplicate,
    extract_phones,
    find_matches,
    is_known_agency_phone,
    normalize_phone,
)


def finding(rec_id, contact=None, **extra):
    fields = {'Contact': contact} if contact else {}
    fields.update(extra)
    return {'id': rec_id, 'fields': fields}


class TestNormalizePhone:

    @pytest.mark.parametrize("raw,expected", [
        ('+62 813-3919-882', '628133919882'),
        ('62 813 3919 882', '628133919882'),
        ('08133919882', '628133919882'),
        ('https://wa.me/628133919882', '628133919882'),
        ('(0813) 391-9882', '628133919882'),
    ])
    def test_same_number_written_differently(self, raw, expected):
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", [None, '', '   ', 'нет телефона', '12345', '@username'])
    def test_rejects_non_phones(self, raw):
        assert normalize_phone(raw) is None

    def test_wa_me_and_raw_number_match(self):
        """Контакт в базе лежит как wa.me, а с баннера приходит сырой номер."""
        assert normalize_phone('https://wa.me/628133919882') == normalize_phone('0813 391 9882')


class TestExtractPhones:

    def test_several_numbers_in_one_field(self):
        contact = 'https://wa.me/628133919882, https://wa.me/628111234567'
        assert extract_phones(contact) == ['628133919882', '628111234567']

    def test_mixed_contacts_keep_only_phones(self):
        assert extract_phones('@sales_bali, https://wa.me/628133919882') == ['628133919882']

    def test_duplicates_collapse(self):
        assert extract_phones('0813 391 9882, +62 813 391 9882') == ['628133919882']

    def test_empty(self):
        assert extract_phones(None) == []
        assert extract_phones('') == []

    def test_merged_agency_numbers_with_slashes_and_pipes(self):
        assert extract_phones('+628133919882 / +628111234567') == ['628133919882', '628111234567']
        assert extract_phones('08133919882 | 08111234567') == ['628133919882', '628111234567']
        assert extract_phones('+628133919882+628111234567') == ['628133919882', '628111234567']


class TestFindDuplicates:

    def test_matches_same_phone_across_findings(self):
        existing = [finding('rec1', 'https://wa.me/628133919882')]
        assert find_matches('628133919882', existing)['phone'] == existing

    def test_different_phone_is_not_a_duplicate(self):
        existing = [finding('rec1', 'https://wa.me/628111111111')]
        assert find_matches('628133919882', existing)['phone'] == []

    def test_excludes_the_record_being_checked(self):
        """Находка не должна находить сама себя."""
        existing = [finding('rec_self', 'https://wa.me/628133919882')]
        assert find_matches('628133919882', existing, exclude_id='rec_self')['phone'] == []

    def test_matches_regardless_of_project_name(self):
        """Ключевое правило: имя проекта на решение не влияет."""
        existing = [finding('rec1', 'https://wa.me/628133919882',
                            **{'Project Name': 'Совсем другой проект'})]
        assert len(find_matches('628133919882', existing)['phone']) == 1

    def test_no_phones_means_no_duplicates(self):
        existing = [finding('rec1', 'https://wa.me/628133919882')]
        assert find_matches('', existing)['phone'] == []

    def test_findings_without_contact_are_skipped(self):
        existing = [finding('rec1'), finding('rec2', 'https://wa.me/628133919882')]
        assert [m['id'] for m in find_matches('628133919882', existing)['phone']] == ['rec2']

    def test_reports_every_match(self):
        existing = [
            finding('rec1', 'https://wa.me/628133919882'),
            finding('rec2', '0813 391 9882'),
        ]
        assert len(find_matches('628133919882', existing)['phone']) == 2


class TestMessages:

    def test_reason_mentions_phone_and_finding(self):
        matches = [finding('rec1', 'https://wa.me/628133919882', Id=42)]
        reason = describe_duplicate(matches, ['628133919882'])
        assert '628133919882' in reason
        assert '42' in reason

    def test_reason_empty_without_matches(self):
        assert describe_duplicate([], ['628133919882']) == ""

    def test_notice_names_the_earlier_finding(self):
        matches = [finding('rec1', 'https://wa.me/628133919882', Id=42,
                           **{'Submitted By': '@lister1', 'Submission Time': '2026-07-12T10:00:00.000Z'})]
        text = build_duplicate_notice(matches, ['628133919882'])
        assert '#42' in text
        assert '@lister1' in text
        assert '2026-07-12' in text

    def test_notice_counts_multiple_matches(self):
        matches = [finding('rec1', '0813 391 9882', Id=1),
                   finding('rec2', '0813 391 9882', Id=2)]
        assert 'Всего совпадений: 2' in build_duplicate_notice(matches, ['628133919882'])

    def test_notice_survives_missing_metadata(self):
        text = build_duplicate_notice([finding('rec1', '0813 391 9882')], ['628133919882'])
        assert 'rec1' in text


class TestAgentPhoneTrap:
    """
    Ловушка, на которую указал владелец: листер может снять несколько баннеров,
    поставленных агентом. Агент рекламирует объекты РАЗНЫХ застройщиков, а
    телефон у него один. Случай редкий, но слить такие находки означало бы
    потерять проект.

    Разделение простое: совпадение телефона отвечает на вопрос «тот же
    контакт?», но не на вопрос «тот же проект?». Признак агента — на одном
    номере накопилось несколько разных названий проектов.
    """

    def _match(self, project_name, rec_id='rec1'):
        return finding(rec_id, '0813 391 9882', **{'Project Name': project_name})

    def test_developer_phone_with_one_project_is_a_plain_duplicate(self):
        matches = [self._match('Rise Villas')]
        assert classify_phone_match(matches, 'Rise Villas') == SAME_LEAD

    def test_two_projects_on_one_phone_still_reads_as_one_lead(self):
        """У застройщика бывает несколько своих проектов — это не агент."""
        matches = [self._match('Rise Villas', 'rec1'), self._match('Rise Villas 2', 'rec2')]
        assert classify_phone_match(matches, 'Rise Villas') == SAME_LEAD

    def test_many_different_projects_on_one_phone_looks_like_an_agent(self):
        matches = [
            self._match('Alaya Residences', 'rec1'),
            self._match('Ocean Bay Villas', 'rec2'),
            self._match('Seven Oceans', 'rec3'),
        ]
        assert classify_phone_match(matches, 'CASA OASIS') == POSSIBLE_AGENT

    def test_agent_case_warns_instead_of_calling_it_a_duplicate(self):
        matches = [
            self._match('Alaya Residences', 'rec1'),
            self._match('Ocean Bay Villas', 'rec2'),
            self._match('Seven Oceans', 'rec3'),
        ]
        reason = describe_duplicate(matches, ['628133919882'], 'CASA OASIS')
        assert 'агент' in reason.lower()
        assert 'нельзя' in reason.lower()

        notice = build_duplicate_notice(matches, ['628133919882'], 'CASA OASIS')
        assert 'не дубль' in notice.lower()

    def test_plain_duplicate_keeps_the_calm_wording(self):
        matches = [self._match('Rise Villas')]
        notice = build_duplicate_notice(matches, ['628133919882'], 'Rise Villas')
        assert 'Похоже на дубль' in notice
        assert 'агент' not in notice.lower()

    def test_unnamed_findings_are_not_mistaken_for_an_agent(self):
        """Пока названий нет, судить не о чем — это обычное совпадение контакта."""
        matches = [finding('rec1', '0813 391 9882'), finding('rec2', '0813 391 9882')]
        assert classify_phone_match(matches, None) == SAME_LEAD


class TestKnownAgencyPhones:
    """
    Справочник Agencies собран из рабочей воронки партнёров: 328 агентств,
    телефон распознан у 229. Список заведомо неполный — в агентстве десяток
    агентов, а номеров записано один-два. Поэтому попадание в него закрывает
    вопрос сразу, а отсутствие ничего не доказывает и решение принимается по
    накопленным названиям проектов.
    """

    def test_known_agency_phone_decides_immediately(self, monkeypatch):
        monkeypatch.setattr('app.dedup.is_known_agency_phone', lambda phones: True)
        matches = [finding('rec1', '0813 391 9882', **{'Project Name': 'Rise Villas'})]
        assert classify_phone_match(matches, 'Rise Villas', ['628133919882']) == POSSIBLE_AGENT

    def test_unknown_phone_falls_back_to_project_count(self, monkeypatch):
        monkeypatch.setattr('app.dedup.is_known_agency_phone', lambda phones: False)
        matches = [finding('rec1', '0813 391 9882', **{'Project Name': 'Rise Villas'})]
        assert classify_phone_match(matches, 'Rise Villas', ['628133919882']) == SAME_LEAD

    def test_lookup_failure_does_not_break_classification(self, monkeypatch):
        """Недоступный Airtable не должен ронять разбор находки."""
        def boom():
            raise RuntimeError('Airtable недоступен')
        monkeypatch.setattr('app.airtable_client.get_agency_phones', boom)
        assert is_known_agency_phone(['628133919882']) is False

    def test_no_phones_means_no_agency_hit(self):
        assert is_known_agency_phone([]) is False
