"""
Очистка поля в Airtable — отдельная задача от «не писать пустоту».

robust_airtable_op вырезает из payload все пустые значения: это защита от
того, чтобы разбор с полупустым результатом не затирал уже накопленные данные.
Но под ту же гребёнку попадал и Gaps, который upsert_project СПЕЦИАЛЬНО ставит
в "", когда пропусков не осталось.

Из-за этого Gaps нельзя было очистить ни при каких данных: заполнив последний
пробел, карточка получала Status='Verified' и одновременно продолжала
показывать старый список вопросов. Именно этот список по регламенту уходит
застройщику — то есть мы спросили бы про то, что уже знаем.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.airtable_client import robust_airtable_op


def _echo(**kwargs):
    """Заглушка вместо таблицы Airtable: возвращает то, что до неё дошло."""
    return kwargs


class TestEmptyValuesAreStripped:
    """Базовое поведение не меняется: пустота обычных полей не пишется."""

    def test_none_is_stripped(self):
        sent = robust_airtable_op(_echo, fields={'District': None, 'Project Name': 'X'})
        assert 'District' not in sent['fields']
        assert sent['fields']['Project Name'] == 'X'

    def test_blank_string_is_stripped(self):
        sent = robust_airtable_op(_echo, fields={'District': '   ', 'Project Name': 'X'})
        assert 'District' not in sent['fields']

    def test_zero_survives(self):
        """0 — законная цена, а не пустота."""
        sent = robust_airtable_op(_echo, fields={'Price From (USD)': 0})
        assert sent['fields']['Price From (USD)'] == 0


class TestGapsCanBeCleared:
    """Gaps обязан уметь опустошаться — иначе Verified противоречит сам себе."""

    def test_empty_gaps_reaches_airtable(self):
        sent = robust_airtable_op(_echo, fields={'Gaps': '', 'Project Name': 'X'})
        assert 'Gaps' in sent['fields'], (
            "Gaps='' обязан дойти до Airtable: это осознанная очистка, "
            "а не отсутствие данных"
        )
        assert sent['fields']['Gaps'] == ''

    def test_empty_gaps_reaches_airtable_via_positional_fields(self):
        """upsert_* передаёт fields именованным аргументом, но ветка есть и вторая."""
        sent = robust_airtable_op(_echo, fields={'Gaps': ''})
        assert sent['fields'].get('Gaps') == ''

    def test_filled_gaps_still_pass_through(self):
        sent = robust_airtable_op(_echo, fields={'Gaps': 'зонирование земли'})
        assert sent['fields']['Gaps'] == 'зонирование земли'

    def test_none_gaps_is_still_stripped(self):
        """None — это «нечего сказать», а не «очистить»."""
        sent = robust_airtable_op(_echo, fields={'Gaps': None, 'Project Name': 'X'})
        assert 'Gaps' not in sent['fields']
