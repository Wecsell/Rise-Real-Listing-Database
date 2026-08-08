# -*- coding: utf-8 -*-
"""
Координаты из ссылки на Google Maps.

Регресс 08.08.2026 (владелец: «ссылка на гугл-карты никогда не парсится»):
промпт разбора запрещал угадывать координаты из короткой ссылки и обещал, что
их раскроет «человек или инструмент», но инструмента не существовало, а хосты
карт вдобавок не проходили allow-list url_safety. Ссылка на локацию не
превращалась в координаты ни разу.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.maps_link import extract_coordinates, is_short_maps_link
from app.url_safety import (
    MAPS_HOST_PATTERNS,
    configured_trusted_hosts,
    host_matches_allowed_pattern,
)


class TestExtractCoordinates:
    """Порядок на выходе — канон поля карты: "долгота, широта"."""

    def test_at_form(self):
        url = 'https://www.google.com/maps/place/Oasis/@-8.6591737,115.1449789,1123m'
        assert extract_coordinates(url) == '115.1449789, -8.6591737'

    def test_query_form(self):
        assert extract_coordinates('https://www.google.com/maps?q=-8.628787,115.11273') \
            == '115.11273, -8.628787'

    def test_3d4d_form_survives_recentering(self):
        """!3d/!4d — координаты самой точки, а не центра карты."""
        url = 'https://www.google.com/maps/data=!3m1!4b1!4m2!3d-8.65!4d115.14'
        assert extract_coordinates(url) == '115.14, -8.65'

    def test_degrees_in_place_name_do_not_confuse(self):
        url = ("https://www.google.com/maps/place/8%C2%B049'37.4%22S+115%C2%B012'11.4%22E/"
               "@-8.827066,115.203166,17z")
        assert extract_coordinates(url) == '115.203166, -8.827066'

    def test_short_link_yields_nothing_without_resolving(self):
        url = 'https://maps.app.goo.gl/riPHmroacDZuFh7u6'
        assert is_short_maps_link(url) is True
        assert extract_coordinates(url) is None

    def test_garbage_is_none(self):
        for bad in (None, '', 'не ссылка', 'https://example.com/'):
            assert extract_coordinates(bad) is None

    def test_out_of_range_pair_is_rejected(self):
        assert extract_coordinates('https://www.google.com/maps?q=-999.0,115.1') is None


class TestMapsHostsAreScopedNotGlobal:
    """
    Хосты карт нужны только резолверу и НЕ добавляются в общий список доверия:
    иначе расширение делается ради одной задачи, а действует на весь код.
    """

    def test_maps_hosts_match_the_pattern_set(self):
        for host in ('maps.app.goo.gl', 'www.google.com', 'maps.google.com'):
            assert host_matches_allowed_pattern(host, MAPS_HOST_PATTERNS) is True

    def test_maps_hosts_are_not_globally_trusted(self):
        trusted = configured_trusted_hosts()
        assert host_matches_allowed_pattern('maps.app.goo.gl', trusted) is False

    def test_unrelated_host_never_matches(self):
        assert host_matches_allowed_pattern('evil.example.com', MAPS_HOST_PATTERNS) is False


class TestSearchFormInPath:
    """
    Короткая ссылка на точку без привязки к месту раскрывается в
    /maps/search/lat,+lng — координаты лежат в ПУТИ, ни @, ни ?q= там нет.

    Регресс 08.08.2026: The Sense не отдавал координаты, хотя соседние проекты
    Nuanu отдавали — те шли через форму @.
    """

    def test_search_path_form(self):
        url = ('https://www.google.com/maps/search/-8.626060,+115.102868'
               '?entry=tts&g_ep=EgoyMDI1MTAxNC4wIPu8ASoASAFQAw%3D%3D')
        assert extract_coordinates(url) == '115.102868, -8.62606'

    def test_search_path_with_encoded_space(self):
        url = 'https://www.google.com/maps/search/-8.65,%20115.14'
        assert extract_coordinates(url) == '115.14, -8.65'

    def test_dir_path_form(self):
        assert extract_coordinates('https://www.google.com/maps/dir/-8.65,115.14') \
            == '115.14, -8.65'
