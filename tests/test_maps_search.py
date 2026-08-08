# -*- coding: utf-8 -*-
"""
Поиск точки проекта по имени в Google Maps.

Владелец, 08.08.2026: «если это не большой проект, а локальная вилла, искать
поиском — плохая затея: с вероятностью 90% найдётся что-то другое, на Бали
куча вилл с одинаковыми названиями». Поэтому проверяется в первую очередь
УМЕНИЕ ОТКАЗАТЬСЯ, а не умение найти.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.maps_search import (
    choose_place,
    in_bounds,
    is_risky_name,
    name_matches,
    parse_place,
)

# Реальная выдача по запросу «The Pavilions Nuanu Bali» (08.08.2026).
PAVILIONS_RESULTS = [
    {'name': 'Nuanu Gate',
     'url': 'https://www.google.com/maps/place/Nuanu+Gate/data=!4m7!3m6!8m2!3d-8.6304965!4d115.1004586'},
    {'name': 'OXO The Pavilions',
     'url': 'https://www.google.com/maps/place/OXO+The+Pavilions/data=!4m7!3m6!8m2!3d-8.6209779!4d115.1001196'},
    # Ловушка: одноимённый отель в Сануре, в 20 км от нужного места.
    {'name': 'The Pavilions Bali',
     'url': 'https://www.google.com/maps/place/The+Pavilions+Bali/data=!4m10!3m9!8m2!3d-8.6886706!4d115.2627628'},
]


class TestViewportIsNotAPlace:
    """Адрес страницы выдачи содержит центр экрана, а не объект."""

    def test_search_viewport_is_rejected(self):
        url = ('https://www.google.com/maps/search/The+Pavilions+Nuanu+Bali/'
               '@-8.6538964,115.1792179,13z/data=!3m1!4b1')
        assert parse_place(url) is None

    def test_place_card_is_accepted(self):
        p = parse_place('https://www.google.com/maps/place/OXO+The+Pavilions/'
                        'data=!4m7!3m6!8m2!3d-8.6209779!4d115.1001196')
        assert p['lat'] == -8.6209779 and p['lng'] == 115.1001196
        assert p['from'] == 'place'

    def test_zoomed_at_form_is_accepted(self):
        p = parse_place('https://www.google.com/maps/place/Ecoverse+by+OXO/'
                        '@-8.6244449,115.1050695,17z/data=!3m1!4b1')
        assert p['lat'] == -8.6244449
        assert p['name'].startswith('Ecoverse')


class TestBounds:
    def test_district_bounds_reject_other_side_of_island(self):
        # Санур попадает в границы Бали, но не в границы Чангу.
        assert in_bounds(-8.6886706, 115.2627628) is True
        assert in_bounds(-8.6886706, 115.2627628, 'nuanu') is False

    def test_point_inside_its_district(self):
        assert in_bounds(-8.6209779, 115.1001196, 'nuanu') is True


class TestNameGate:
    def test_all_significant_words_must_be_present(self):
        assert name_matches('OXO The Pavilions', 'The Pavilions') is True
        assert name_matches('Nuanu Gate', 'The Pavilions') is False

    def test_generic_words_do_not_count(self):
        # «Villa»/«Bali» есть у каждой второй — по ним совпадать нельзя.
        assert name_matches('Some Other Villa Bali', 'Villa Bali') is False

    def test_single_word_name_is_risky(self):
        assert is_risky_name('Zen') is True
        assert is_risky_name('Solana') is True
        # «The Pavilions» тоже рискованное: «the» ничего не различает, остаётся
        # одно слово — и тёзка в Сануре тому подтверждение. Родовые хвосты
        # («Residences», «Estates») слово тоже не добавляют.
        assert is_risky_name('The Pavilions') is True
        assert is_risky_name('Flower Estates') is True

    def test_multiword_name_is_not_risky(self):
        assert is_risky_name('Black Sands Oasis') is False
        assert is_risky_name('Nyanyi Village Bali') is False


class TestChoosePlace:
    def test_picks_the_developer_confirmed_result(self):
        r = choose_place(PAVILIONS_RESULTS, 'The Pavilions',
                         district='nuanu', developer='OXO Living')
        assert r is not None
        assert r['coordinates'] == '115.1001196, -8.6209779'
        assert 'OXO' in r['name']

    def test_sanur_namesake_alone_is_refused(self):
        """Без района одноимённый отель прошёл бы — с районом отсекается."""
        only_trap = [PAVILIONS_RESULTS[2]]
        assert choose_place(only_trap, 'The Pavilions', district='nuanu') is None

    def test_risky_name_without_corroboration_is_refused(self):
        cand = [{'name': 'Zen Villa Ubud',
                 'url': 'https://www.google.com/maps/place/Zen+Villa+Ubud/data=!8m2!3d-8.5!4d115.26'}]
        assert choose_place(cand, 'Zen') is None

    def test_risky_name_passes_when_district_known(self):
        cand = [{'name': 'Zen Villa Ubud',
                 'url': 'https://www.google.com/maps/place/Zen+Villa+Ubud/data=!8m2!3d-8.5!4d115.26'}]
        r = choose_place(cand, 'Zen', district='ubud')
        assert r is not None and r['coordinates'] == '115.26, -8.5'

    def test_two_different_points_without_developer_are_refused(self):
        """Два разных места подошли — выбирать наугад нельзя."""
        cand = [
            {'name': 'Serenity Villas', 'url': 'https://www.google.com/maps/place/Serenity+Villas/data=!8m2!3d-8.51!4d115.25'},
            {'name': 'Serenity Villas Ubud', 'url': 'https://www.google.com/maps/place/Serenity+Villas+Ubud/data=!8m2!3d-8.53!4d115.27'},
        ]
        assert choose_place(cand, 'Serenity Villas', district='ubud') is None

    def test_empty_input_is_refused_not_crash(self):
        assert choose_place([], 'Anything') is None
        assert choose_place(None, 'Anything') is None


class TestExactNameBreaksTheTie:
    """
    По «Bingin Elements» в выдаче рядом стоят соседние виллы «Elements A6 Villa
    Bingin» — в них есть оба значащих слова, поэтому по словам они проходят.
    Спор решает точное совпадение имени (08.08.2026).
    """

    CAND = [
        {'name': 'Bingin Elements',
         'url': 'https://www.google.com/maps/place/Bingin+Elements/data=!8m2!3d-8.8216875!4d115.1144375'},
        {'name': 'Elements A6 Villa Bingin - Entire Place',
         'url': 'https://www.google.com/maps/place/Elements+A6/data=!8m2!3d-8.8026762!4d115.1195908'},
        {'name': 'Elements B2 Villa Bingin',
         'url': 'https://www.google.com/maps/place/Elements+B2/data=!8m2!3d-8.8125887!4d115.1217652'},
    ]

    def test_exact_name_wins(self):
        r = choose_place(self.CAND, 'Bingin Elements', district='uluwatu')
        assert r is not None
        assert r['coordinates'] == '115.1144375, -8.8216875'

    def test_without_exact_match_still_refuses(self):
        r = choose_place(self.CAND[1:], 'Bingin Elements', district='uluwatu')
        assert r is None
