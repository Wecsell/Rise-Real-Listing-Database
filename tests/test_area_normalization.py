"""
Нормализация района.

История бага: AREA_ALIASES вел половину значений на названия, которых нет в
селекте Airtable (Berawa, Bingin, Bukit, Pecatu, Mengwi, Karangasem против
Karengasem в базе). sanitize_area их не находил и возвращал None, а upsert_project
молча делал fields.pop('Район'). Итог — 33 проекта из 47 без района.
"""
import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.airtable_client import sanitize_area, AREA_ALIASES

# Список, каким он станет в Airtable после заведения недостающих опций
CANONICAL = [
    'Uluwatu', 'Ungasan', 'Nusa Dua', 'Jimbaran', 'Bukit', 'Kuta', 'Seminyak',
    'Canggu', 'Seseh', 'Cemagi', 'Nuanu', 'Kedungu', 'Tabanan', 'Sanur',
    'Denpasar', 'Ubud', 'Karangasem', 'Amed', 'Lovina', 'Munduk', 'Kintamani',
    'Bedugul', 'Lombok', 'Sumba', 'Nusa Penida',
    # живут в базе, хотя в основной список районов не входят
    'Kerobokan', 'Umalas', 'Pererenan',
]


class TestAliasTargetsAreValid(unittest.TestCase):

    def test_every_alias_points_to_an_existing_district(self):
        """Главный инвариант: алиас не может вести на несуществующее значение."""
        broken = {
            alias: target
            for alias, target in AREA_ALIASES.items()
            if target not in CANONICAL
        }
        self.assertEqual(broken, {},
                         f"Эти алиасы ведут на значения, которых нет в Airtable: {broken}")


class TestDistrictFolding(unittest.TestCase):

    def _area(self, raw):
        return sanitize_area(raw, CANONICAL, is_project=True)

    def test_berawa_folds_to_canggu(self):
        self.assertEqual(self._area('Berawa'), 'Canggu')
        self.assertEqual(self._area('Берава'), 'Canggu')

    def test_bingin_folds_to_uluwatu(self):
        self.assertEqual(self._area('Bingin'), 'Uluwatu')
        self.assertEqual(self._area('Pecatu'), 'Uluwatu')

    def test_melasti_and_kutuh_fold_to_ungasan(self):
        self.assertEqual(self._area('Melasti'), 'Ungasan')
        self.assertEqual(self._area('Kutuh'), 'Ungasan')

    def test_mengwi_folds_to_tabanan(self):
        self.assertEqual(self._area('Mengwi'), 'Tabanan')

    def test_karangasem_spelling(self):
        self.assertEqual(self._area('Karangasem'), 'Karangasem')
        self.assertEqual(self._area('Карангасем'), 'Karangasem')

    def test_islands_stay_themselves(self):
        self.assertEqual(self._area('Lombok'), 'Lombok')
        self.assertEqual(self._area('Nusa Penida'), 'Nusa Penida')
        self.assertEqual(self._area('Sumba'), 'Sumba')

    def test_northern_districts(self):
        for raw, expected in [('Amed', 'Amed'), ('Lovina', 'Lovina'),
                              ('Munduk', 'Munduk'), ('Kintamani', 'Kintamani'),
                              ('Bedugul', 'Bedugul'), ('Denpasar', 'Denpasar')]:
            self.assertEqual(self._area(raw), expected)

    def test_alias_found_inside_free_text(self):
        self.assertEqual(self._area('Вилла в районе Берава, Чангу'), 'Canggu')

    def test_comma_separated_falls_back_to_known_part(self):
        self.assertEqual(self._area('Penestanan, Ubud'), 'Ubud')


class TestBukitFallback(unittest.TestCase):
    """Букит — не район, а весь южный полуостров: ставим как метку на ручную правку."""

    def test_unidentified_bukit_becomes_bukit(self):
        self.assertEqual(sanitize_area('Bukit', CANONICAL, is_project=True), 'Bukit')
        self.assertEqual(sanitize_area('Букит', CANONICAL, is_project=True), 'Bukit')

    def test_specific_bukit_location_wins_over_fallback(self):
        """Если район опознан конкретно — 'Bukit' ставить не надо."""
        self.assertEqual(sanitize_area('Bingin, Bukit', CANONICAL, is_project=True), 'Uluwatu')

    def test_unknown_area_still_returns_none(self):
        self.assertIsNone(sanitize_area('Somewhere In Java', CANONICAL, is_project=True))


if __name__ == '__main__':
    unittest.main()
