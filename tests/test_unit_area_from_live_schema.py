"""
Район юнита сверяется с живым селектом Airtable, а не с копией списка в коде.

Найдено 02.08.2026 на листинге Bali Baza: у 9 типологий Baza Kedungu район
'Kedungu' молча вычищался с сообщением "Area 'Kedungu' is invalid and was
cleared". В живом селекте Units.Area значение 'Kedungu' ЕСТЬ - его не было в
захардкоженном VALID_UNIT_AREAS, который разошёлся с базой.

Асимметрия, из-за которой это жило незамеченным: для Projects район уже
сверялся с живой базой через get_valid_project_areas(), а для Units - с
константой. Тот же класс расхождения схемы, ради которого в проекте написан
app/schema_check.py.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.airtable_client as ac


class TestUnitAreasComeFromLiveSchema:

    def test_kedungu_is_accepted_when_live_select_has_it(self):
        """
        Регрессия на конкретную потерю: 'Kedungu' есть в живом селекте и
        обязан пережить нормализацию, даже если в коде его забыли.
        """
        live = ['Canggu', 'Seseh', 'Kedungu', 'Nuanu']
        with patch.object(ac, 'get_select_options', return_value=live):
            assert ac.sanitize_area('Kedungu', ac.get_valid_unit_areas()) == 'Kedungu'

    def test_value_absent_from_live_select_is_still_rejected(self):
        """
        Обратная проверка: живой список не означает «принимаем всё». Значения,
        которого в селекте нет, быть не должно - Airtable отверг бы запись.
        'Kerobokan' - реальный случай: он есть в Projects.District, но в
        Units.Area его нет.
        """
        live = ['Canggu', 'Seseh', 'Kedungu', 'Nuanu']
        with patch.object(ac, 'get_select_options', return_value=live):
            assert ac.sanitize_area('Kerobokan', ac.get_valid_unit_areas()) is None

    def test_falls_back_to_hardcoded_list_when_schema_unreadable(self):
        """Схема не прочиталась - работаем по запасному списку, а не роняем запись."""
        with patch.object(ac, 'get_select_options', side_effect=lambda t, f, fb=None: fb or []):
            areas = ac.get_valid_unit_areas()
            assert 'Canggu' in areas

    def test_unit_areas_are_read_from_units_table_not_projects(self):
        """
        Списки районов у Projects и Units РАЗНЫЕ (в Units нет Kerobokan, в
        Projects нет Mengwi). Чтение не из той таблицы вернуло бы чужой список.
        """
        seen = {}

        def fake(table, field, fallback=None):
            seen['table'] = table
            seen['field'] = field
            return ['Canggu']

        with patch.object(ac, 'get_select_options', side_effect=fake):
            ac.get_valid_unit_areas()

        assert seen['table'] == 'Units'
        assert seen['field'] == 'Area'
