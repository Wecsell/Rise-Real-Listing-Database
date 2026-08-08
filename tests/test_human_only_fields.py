"""
Поля ручной проверки код не пишет.

Смысл проверки: галочка, которую бот может снять или поставить, ничего не
гарантирует. 'Active' — ворота видимости в интерфейсе, поэтому её значение
должен определять только человек.
"""
import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.airtable_client import HUMAN_ONLY_FIELDS, strip_human_only_fields


class TestStripHumanOnlyFields(unittest.TestCase):

    def test_active_is_removed(self):
        fields = {'Project Name': 'Y-WAY', 'Active': True}
        strip_human_only_fields(fields)
        self.assertNotIn('Active', fields)
        self.assertEqual(fields['Project Name'], 'Y-WAY')

    def test_active_false_is_also_removed(self):
        """Снятие галочки — такое же вмешательство, как её постановка:
        бот не должен обнулять чужую проверку."""
        fields = {'Active': False}
        strip_human_only_fields(fields)
        self.assertEqual(fields, {})

    def test_other_fields_untouched(self):
        fields = {'Status': 'Verified', 'Gaps': '', 'Key': 'yway__1br'}
        before = dict(fields)
        strip_human_only_fields(fields)
        self.assertEqual(fields, before)

    def test_returns_same_dict(self):
        """Функция правит payload на месте — вызывающий код полагается на это."""
        fields = {'Active': True}
        self.assertIs(strip_human_only_fields(fields), fields)

    def test_active_is_declared_human_only(self):
        self.assertIn('Active', HUMAN_ONLY_FIELDS)


class TestUpsertNeverWritesActive(unittest.TestCase):
    """
    Регресс: 'Active' не должен попадать в Airtable из payload проекта или
    юнита. Проверяется на самих upsert_*, а не только на хелпере, — иначе
    защиту можно случайно выкинуть из вызова и тесты этого не заметят.
    """

    def test_upsert_project_calls_the_guard(self):
        import inspect
        from app import airtable_client
        src = inspect.getsource(airtable_client.upsert_project)
        self.assertIn('strip_human_only_fields', src)

    def test_upsert_unit_calls_the_guard(self):
        import inspect
        from app import airtable_client
        src = inspect.getsource(airtable_client.upsert_unit)
        self.assertIn('strip_human_only_fields', src)

    def test_guard_runs_before_the_write(self):
        """Порядок важен: снятие поля после формирования payload бесполезно."""
        import inspect
        from app import airtable_client
        for fn in (airtable_client.upsert_project, airtable_client.upsert_unit):
            src = inspect.getsource(fn)
            guard_at = src.index('strip_human_only_fields')
            # Первое обращение к таблице на запись идёт после защиты.
            for write_call in ('table.update', 'table.create'):
                if write_call in src:
                    self.assertLess(guard_at, src.index(write_call),
                                    f"{fn.__name__}: защита стоит после {write_call}")


if __name__ == '__main__':
    unittest.main()
