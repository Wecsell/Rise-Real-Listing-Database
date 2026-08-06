"""
Вычисляемые поля не уезжают в Airtable.

Цена ошибки здесь не «потеряли одно поле», а «не сохранилась вся запись»:
Airtable отвечает 422 на весь апдейт, если в нём есть хоть один lookup или
формула. Так уже ронялись юниты из-за 'Unit ID', а Projects.'Developer Link'
до 06.08.2026 лежал в схеме ответа модели и мог прилететь в любой момент.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.airtable_client import COMPUTED_FIELD_TYPES, strip_computed_fields


class TestStripComputedFields(unittest.TestCase):

    @patch('app.airtable_client.get_field_type')
    def test_lookup_and_formula_are_dropped(self, mock_type):
        types = {
            'Developer Link': 'multipleLookupValues',
            'Unit ID': 'formula',
            'Project Name': 'singleLineText',
            'Price from(USD)': 'currency',
        }
        mock_type.side_effect = lambda table, name: types.get(name)

        fields = dict.fromkeys(types, 'x')
        strip_computed_fields('Projects', fields)

        self.assertEqual(set(fields), {'Project Name', 'Price from(USD)'})

    @patch('app.airtable_client.get_field_type')
    def test_unknown_type_is_kept(self, mock_type):
        """Схема недоступна -> тип None. Предохранитель не должен вычищать
        данные из-за того, что метаданные не прочитались."""
        mock_type.return_value = None
        fields = {'Project Name': 'Y-WAY', 'Something New': 1}
        strip_computed_fields('Projects', fields)
        self.assertEqual(set(fields), {'Project Name', 'Something New'})

    @patch('app.airtable_client.get_field_type')
    def test_returns_same_dict(self, mock_type):
        mock_type.return_value = 'formula'
        fields = {'Unit ID': 'x'}
        self.assertIs(strip_computed_fields('Units', fields), fields)

    def test_lookup_types_are_covered(self):
        for t in ('formula', 'lookup', 'multipleLookupValues', 'rollup', 'autoNumber'):
            self.assertIn(t, COMPUTED_FIELD_TYPES)

    def test_list_matches_schema_check(self):
        """Два списка обязаны совпадать: один отсеивает, другой предупреждает."""
        from app.schema_check import COMPUTED_FIELD_TYPES as CHECK_TYPES
        self.assertEqual(set(COMPUTED_FIELD_TYPES), set(CHECK_TYPES))


class TestGuardIsWiredIntoWrites(unittest.TestCase):

    def test_both_upserts_call_the_guard(self):
        import inspect
        from app import airtable_client
        for fn in (airtable_client.upsert_project, airtable_client.upsert_unit):
            src = inspect.getsource(fn)
            self.assertIn('strip_computed_fields', src, f"{fn.__name__} без предохранителя")

    def test_guard_runs_before_the_write(self):
        import inspect
        from app import airtable_client
        for fn in (airtable_client.upsert_project, airtable_client.upsert_unit):
            src = inspect.getsource(fn)
            guard_at = src.index('strip_computed_fields')
            for write_call in ('table.update', 'table.create'):
                if write_call in src:
                    self.assertLess(guard_at, src.index(write_call),
                                    f"{fn.__name__}: предохранитель после {write_call}")


class TestDeveloperLinkNotInModelSchema(unittest.TestCase):
    """
    Модель не должна иметь возможности вернуть lookup-поле: оно не описывает
    ничего нового (имя застройщика и так в Developer), а стоит целой записи.
    """

    def test_project_schema_has_no_developer_link(self):
        from app.gemini_parser import ProjectData
        aliases = {f.alias or n for n, f in ProjectData.model_fields.items()}
        self.assertNotIn('Developer Link', aliases)



class TestGuardIsTheLastStepBeforeWrite(unittest.TestCase):
    """
    Регресс 06.08.2026: владелец превратил Units.'Group with agency' в lookup
    на Source, а предохранитель стоял в тексте ВЫШЕ присваивания этого поля.
    Поле существует, поэтому field_exists его пропускал, и каждая запись юнита
    с известным чатом ушла бы в 422.

    Отсюда правило: strip_computed_fields идёт последним, после всех
    присваиваний, иначе он защищает только часть payload.
    """

    def test_no_field_is_assigned_after_the_guard(self):
        import inspect
        import re
        from app import airtable_client

        for fn in (airtable_client.upsert_project, airtable_client.upsert_unit):
            src = inspect.getsource(fn)
            tail = src[src.rindex('strip_computed_fields'):]
            # После предохранителя присваивания в payload быть не должно.
            leftovers = re.findall(r"fields\[['\"]([^'\"]+)['\"]\]\s*=", tail)
            self.assertEqual(leftovers, [],
                             f"{fn.__name__}: после предохранителя пишутся поля {leftovers}")

    def test_group_with_agency_is_never_written(self):
        """Поле выводится из Source — код его не заполняет."""
        import inspect
        from app import airtable_client
        src = inspect.getsource(airtable_client.upsert_unit)
        self.assertNotIn("fields['Group with agency'] =", src)

if __name__ == '__main__':
    unittest.main()
