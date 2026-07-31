"""
Сопоставление находок по всем видам контакта: сайт, аккаунт, телефон.

Правило владельца: совпадение сайта — стопроцентная гарантия дубля.
Собственный домен не бывает общим, в отличие от телефона: тот может
принадлежать агенту, который рекламирует объекты разных застройщиков.
Поэтому виды контактов имеют разный вес, а не складываются в один признак.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.dedup import (
    POSSIBLE_AGENT,
    SAME_COMPANY,
    SAME_LEAD,
    build_contact_notice,
    classify_contact_match,
    describe_contact_match,
    extract_social_handles,
    extract_websites,
    find_matches,
)
from app.phone_formatter import COUNTRY_CODES, format_international, split_country_code


def finding(rec_id, contact, **extra):
    fields = {'Contact': contact}
    fields.update(extra)
    return {'id': rec_id, 'fields': fields}


class TestWebsiteExtraction:

    @pytest.mark.parametrize("raw,expected", [
        ('cemagirock.com', ['cemagirock.com']),
        ('https://www.cemagirock.com', ['cemagirock.com']),
        ('https://oceanbayvillas-cemagi.com/villas?ref=1', ['oceanbayvillas-cemagi.com']),
        ('info@cemagirock.com', ['cemagirock.com']),
        ('CEMAGIROCK.COM', ['cemagirock.com']),
    ])
    def test_domains_are_normalised(self, raw, expected):
        assert extract_websites(raw) == expected

    @pytest.mark.parametrize("raw", [
        'https://wa.me/628133919882',
        'https://t.me/rise_dev',
        'https://instagram.com/casaoasis_',
        'https://drive.google.com/file/d/abc/view',
        'https://bit.ly/xyz',
    ])
    def test_platform_domains_are_ignored(self, raw):
        """Площадка принадлежит сервису, а не компании — доказательством не является."""
        assert extract_websites(raw) == []

    def test_empty(self):
        assert extract_websites(None) == []
        assert extract_websites('нет сайта') == []


class TestHandleExtraction:

    @pytest.mark.parametrize("raw,expected", [
        ('@sales_bali', ['tg:sales_bali']),
        ('https://t.me/rise_dev', ['tg:rise_dev']),
        ('https://instagram.com/casaoasis_', ['ig:casaoasis_']),
        ('@RemaxThrone, 081 805 629 289', ['tg:remaxthrone']),
    ])
    def test_handles_are_prefixed_by_platform(self, raw, expected):
        assert extract_social_handles(raw) == expected

    def test_same_name_on_two_platforms_stays_distinct(self):
        """@rise в телеграме и @rise в инстаграме — разные контакты."""
        handles = extract_social_handles('https://t.me/rise https://instagram.com/rise')
        assert 'tg:rise' in handles
        assert 'ig:rise' in handles


class TestWebsiteIsDecisive:

    def test_website_match_means_same_company(self):
        existing = [finding('rec1', 'https://cemagirock.com, +62 811 111 1111')]
        by_kind = find_matches('info@cemagirock.com, +62 999 999 9999', existing)
        assert len(by_kind['website']) == 1
        assert classify_contact_match(by_kind) == SAME_COMPANY

    def test_website_wins_over_the_agent_signal(self):
        """
        Даже если телефон выглядит агентским, совпавший домен решает: это одна
        и та же компания.
        """
        existing = [
            finding('rec1', 'https://cemagirock.com', **{'Project Name': 'A'}),
            finding('rec2', 'https://cemagirock.com', **{'Project Name': 'B'}),
            finding('rec3', 'https://cemagirock.com', **{'Project Name': 'C'}),
        ]
        by_kind = find_matches('https://cemagirock.com', existing)
        assert classify_contact_match(by_kind, 'D') == SAME_COMPANY

    def test_handle_match_also_means_same_company(self):
        existing = [finding('rec1', '@rise_dev_sales')]
        by_kind = find_matches('@rise_dev_sales', existing)
        assert classify_contact_match(by_kind) == SAME_COMPANY

    def test_phone_only_match_stays_a_lead(self):
        existing = [finding('rec1', '+62 813 391 9882', **{'Project Name': 'Rise Villas'})]
        by_kind = find_matches('+62 813 391 9882', existing)
        assert by_kind['website'] == []
        assert classify_contact_match(by_kind, 'Rise Villas') == SAME_LEAD

    def test_different_domains_do_not_match(self):
        existing = [finding('rec1', 'https://cemagirock.com')]
        by_kind = find_matches('https://oceanbayvillas-cemagi.com', existing)
        assert by_kind['website'] == []

    def test_record_being_checked_is_excluded(self):
        existing = [finding('self', 'https://cemagirock.com')]
        by_kind = find_matches('https://cemagirock.com', existing, exclude_id='self')
        assert by_kind['website'] == []


class TestMessages:

    def test_reason_explains_the_website_rule(self):
        existing = [finding('rec1', 'https://cemagirock.com', Id=7)]
        by_kind = find_matches('https://cemagirock.com', existing)
        reason = describe_contact_match(by_kind, 'https://cemagirock.com')
        assert 'cemagirock.com' in reason
        assert 'тот же застройщик' in reason

    def test_notice_calls_a_website_match_a_definite_duplicate(self):
        existing = [finding('rec1', 'https://cemagirock.com', Id=7)]
        by_kind = find_matches('https://cemagirock.com', existing)
        notice = build_contact_notice(by_kind, 'https://cemagirock.com')
        assert 'Точно дубль' in notice
        assert '#7' in notice

    def test_no_matches_gives_no_notice(self):
        by_kind = find_matches('https://example-unique.com', [])
        assert build_contact_notice(by_kind, 'https://example-unique.com') == ''
        assert describe_contact_match(by_kind, 'https://example-unique.com') == ''


class TestInternationalFormat:
    """
    Голая строка 6281999599998 читается как абракадабра: непонятно, где
    кончается код страны. Пробел после кода снимает вопрос.
    """

    @pytest.mark.parametrize("raw,expected", [
        ('628133919882', '+62 8133919882'),
        ('79103270339', '+7 9103270339'),
        ('971502091446', '+971 502091446'),
        ('996557008800', '+996 557008800'),
        ('+62 813-3919-882', '+62 8133919882'),
    ])
    def test_country_code_is_separated(self, raw, expected):
        assert format_international(raw) == expected

    def test_longest_code_wins(self):
        """996 не должен разбираться как 9 или 99."""
        code, rest = split_country_code('996557008800')
        assert code == '996'
        assert rest == '557008800'

    def test_unknown_code_is_left_whole(self):
        """Выдумывать границу кода нельзя — ошибка сделает номер нерабочим."""
        assert format_international('99900011122') == '+99900011122'

    def test_empty(self):
        assert format_international(None) is None
        assert format_international('') is None

    def test_indonesia_is_present(self):
        assert COUNTRY_CODES['62'] == 'Индонезия'

    def test_formatted_number_still_matches_by_digits(self):
        """Формат для человека не должен ломать сравнение для машины."""
        from app.dedup import normalize_phone
        assert normalize_phone('+62 8133919882') == normalize_phone('08133919882')
