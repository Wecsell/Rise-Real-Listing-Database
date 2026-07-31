"""
Имена-заглушки.

Все примеры ниже — настоящие названия из базы, а не выдуманные. На 47 проектах
13 записей имели вместо названия общее слово или рекламный оборот. Такие имена
хуже, чем отсутствие имени: одинаковые заглушки схлопываются между собой, и две
разные виллы с разных концов острова становятся одной записью.

Заглушка 'Unknown' у застройщика делала обратное: проект привязывался к
фиктивной записи, а когда появлялся настоящий застройщик, строгая иерархия
отказывалась от совпадения и создавала дубль проекта. Так в базе появились
CEMAGI ROCK VILLAS, LASALAHORA RESORT GARDENS и Ocean Bay Villas по два раза,
а под двумя разными записями 'Unknown' скопилось 17 проектов.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.naming import (
    is_generated_placeholder,
    is_placeholder_name,
    next_placeholder_name,
    placeholders_never_match,
)

# Реальные записи из Projects, у которых вместо названия заглушка
REAL_PLACEHOLDERS = [
    'VILLA',
    'DESIGNER VILLA',
    '1-Bed Villa with Pool',
    '1-Bed Villa with Pool (Leasehold)',
    '4-Bedroom Villa',
    'Luxury Villas For Sale',
    'Villa in Canggu',
    'Villa For Lease',
    'Villa Leasehold / Freehold',
    'Unknown Project',
    'Unnamed Project',
    'Lease Land over contract',
    'Local Developer Villa',
    'Property by Imanuel',
    'CEMAGI House',
    'VILLA CEMAGI',
    'Seseh Area Villas',
]

# Реальные названия из той же таблицы, которые трогать нельзя
REAL_NAMES = [
    'CEMAGI ROCK VILLAS', 'Alaya Residences', 'Ocean Bay Villas',
    'Seven Oceans', 'Kizelli', 'Zen', 'Segara', 'UV', 'Anantara',
    'Leo Villas', 'Lumea Villas', 'Gapura Studios', 'The Heights',
    'Umalalang Villas', 'UMA LA LANG VILLAS BALI', 'CASA OASIS',
    'Piece of Paradise', 'Rent Hub', 'KAIANGAN DE MAGI Villa',
    'N Studio Living Seseh Project', 'MARKOV GROUP Project',
    'Ayu Office Development', 'CARE ESTATE', 'Seseh Sense',
    'LASALAHORA RESORT GARDENS', 'Чумаги Апартментс',
    # Найдены на 234-проектной базе после переноса из Base RR New (Copy):
    # декоративное прилагательное рядом с настоящим именем ложно превращало
    # его в заглушку целиком, а не вычиталось как одно мусорное слово.
    'Amali Luxury Residence', 'Beraban Luxury Lofts',
    'Exclusive Villas Collection - The Samahadi',
]


class TestDetection:

    @pytest.mark.parametrize("name", REAL_PLACEHOLDERS)
    def test_real_placeholders_are_detected(self, name):
        assert is_placeholder_name(name) is True

    @pytest.mark.parametrize("name", REAL_NAMES)
    def test_real_names_are_left_alone(self, name):
        assert is_placeholder_name(name) is False

    @pytest.mark.parametrize("name", [None, '', '   ', 'None', 'n/a', '---'])
    def test_empty_variants(self, name):
        assert is_placeholder_name(name) is True

    def test_short_real_name_survives(self):
        """'UV' и 'Zen' — настоящие названия, длина тут ни при чем."""
        assert is_placeholder_name('UV') is False
        assert is_placeholder_name('Zen') is False


class TestGeneration:

    def test_numbers_start_at_one(self):
        assert next_placeholder_name('Villa', []) == 'Unknown Villa 1'

    def test_skips_taken_numbers(self):
        existing = ['Unknown Villa 1', 'Unknown Project 2', 'Alaya Residences']
        assert next_placeholder_name('Villa', existing) == 'Unknown Villa 3'

    def test_developer_placeholder_has_no_kind(self):
        """Для застройщика владелец просил простую нумерацию: Unknown 2."""
        assert next_placeholder_name(None, ['Unknown 1']) == 'Unknown 2'

    def test_numbering_is_shared_across_kinds(self):
        """Номер — идентификатор находки, он не должен повторяться между типами."""
        existing = ['Unknown Villa 1', 'Unknown Apartment 2']
        assert next_placeholder_name('Project', existing) == 'Unknown Project 3'

    def test_generated_names_are_recognised(self):
        for kind in ('Villa', 'Apartment', 'Project', None):
            name = next_placeholder_name(kind, [])
            assert is_generated_placeholder(name), name

    def test_real_names_are_not_generated_placeholders(self):
        assert is_generated_placeholder('Alaya Residences') is False
        assert is_generated_placeholder('Seven Oceans') is False


class TestPlaceholdersNeverMatch:
    """
    Две заглушки — две разные находки, о которых мы ничего не знаем.
    Считать их одним проектом нельзя.
    """

    def test_two_placeholders_are_blocked(self):
        assert placeholders_never_match('VILLA', 'VILLA') is True
        assert placeholders_never_match('Unknown Project', 'DESIGNER VILLA') is True

    def test_placeholder_against_real_name_is_allowed(self):
        """Заглушку можно поднять до настоящего имени, когда оно найдется."""
        assert placeholders_never_match('VILLA', 'Alaya Residences') is False

    def test_two_real_names_are_allowed(self):
        assert placeholders_never_match('Leo Villas', 'Lumea Villas') is False
