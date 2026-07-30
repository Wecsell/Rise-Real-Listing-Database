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
    build_duplicate_notice,
    describe_duplicate,
    extract_phones,
    find_duplicates,
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


class TestFindDuplicates:

    def test_matches_same_phone_across_findings(self):
        existing = [finding('rec1', 'https://wa.me/628133919882')]
        assert find_duplicates(['628133919882'], existing) == existing

    def test_different_phone_is_not_a_duplicate(self):
        existing = [finding('rec1', 'https://wa.me/628111111111')]
        assert find_duplicates(['628133919882'], existing) == []

    def test_excludes_the_record_being_checked(self):
        """Находка не должна находить сама себя."""
        existing = [finding('rec_self', 'https://wa.me/628133919882')]
        assert find_duplicates(['628133919882'], existing, exclude_id='rec_self') == []

    def test_matches_regardless_of_project_name(self):
        """Ключевое правило: имя проекта на решение не влияет."""
        existing = [finding('rec1', 'https://wa.me/628133919882',
                            **{'Project Name': 'Совсем другой проект'})]
        assert len(find_duplicates(['628133919882'], existing)) == 1

    def test_no_phones_means_no_duplicates(self):
        existing = [finding('rec1', 'https://wa.me/628133919882')]
        assert find_duplicates([], existing) == []

    def test_findings_without_contact_are_skipped(self):
        existing = [finding('rec1'), finding('rec2', 'https://wa.me/628133919882')]
        assert [m['id'] for m in find_duplicates(['628133919882'], existing)] == ['rec2']

    def test_reports_every_match(self):
        existing = [
            finding('rec1', 'https://wa.me/628133919882'),
            finding('rec2', '0813 391 9882'),
        ]
        assert len(find_duplicates(['628133919882'], existing)) == 2


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
