"""
Белый список полевого бота.

Раньше проверка была строгим сравнением с единственным значением:
    if str(user_id) != allowed_str.strip()
То есть работать с ботом мог ровно один человек, а в Field Staging не
оставалось следа, кто прислал находку.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.access import describe_user, is_allowed, parse_allowed_users


class FakeUser:
    def __init__(self, id=None, username=None, first_name=None, last_name=None):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class TestParsing:

    def test_single_id_still_works(self):
        """Совместимость: в .env сейчас лежит одно число."""
        assert parse_allowed_users('123456') == {'123456'}

    @pytest.mark.parametrize("raw", [
        '123,456,789',
        '123, 456, 789',
        '123;456;789',
        '123 456 789',
        '123,\n456,\n789',
        '  123 , 456 ,789  ',
    ])
    def test_accepts_the_separators_people_actually_type(self, raw):
        assert parse_allowed_users(raw) == {'123', '456', '789'}

    def test_empty_gives_empty_set(self):
        assert parse_allowed_users('') == set()
        assert parse_allowed_users(None) == set()

    def test_skips_non_numeric_entries(self):
        assert parse_allowed_users('123, @vasya, 456') == {'123', '456'}

    def test_duplicates_collapse(self):
        assert parse_allowed_users('123,123,456') == {'123', '456'}


class TestAuthorization:

    def test_listed_user_is_allowed(self):
        assert is_allowed(456, '123,456,789') is True

    def test_unlisted_user_is_denied(self):
        assert is_allowed(999, '123,456,789') is False

    def test_accepts_int_and_str_ids(self):
        assert is_allowed('456', '123,456') is True
        assert is_allowed(456, '123,456') is True

    def test_empty_whitelist_denies_everyone(self):
        """Пустой список — это «никому», а не «всем»."""
        assert is_allowed(123, '') is False
        assert is_allowed(123, None) is False

    def test_no_substring_matching(self):
        """45 не должен проходить по списку, где есть 456."""
        assert is_allowed(45, '456') is False


class TestSubmitterLabel:

    def test_username_wins(self):
        user = FakeUser(id=1, username='lister1', first_name='Иван')
        assert describe_user(user) == '@lister1'

    def test_falls_back_to_full_name(self):
        user = FakeUser(id=1, first_name='Иван', last_name='Петров')
        assert describe_user(user) == 'Иван Петров'

    def test_first_name_only(self):
        assert describe_user(FakeUser(id=1, first_name='Иван')) == 'Иван'

    def test_falls_back_to_id(self):
        assert describe_user(FakeUser(id=777)) == '777'

    def test_handles_missing_user(self):
        assert describe_user(None) == 'unknown'
