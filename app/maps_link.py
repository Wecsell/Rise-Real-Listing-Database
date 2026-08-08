# -*- coding: utf-8 -*-
"""
Координаты из ссылки на Google Maps.

Зачем отдельный модуль. Промпт разбора (`gemini_parser.EXTRACTION_PROMPT`)
честно запрещает угадывать координаты из короткой ссылки и обещает, что их
«достанет человек или инструмент, раскрыв редирект». Инструмента не было, а
хосты карт вдобавок не проходили `url_safety` — поэтому ссылка на локацию не
превращалась в координаты никогда, ни в одном проекте (владелец, 08.08.2026).

Порядок на выходе — канон поля `Projects.Coordinates(for Map)`: "долгота,
широта", то есть для Бали значение начинается со 115. В самой ссылке Google
отдаёт обратный порядок, и здесь он переворачивается ровно один раз
(см. `naming.swap_coordinates` — там та же пара, но для другого потребителя).
"""
import logging
import re
from typing import Optional

from app.url_safety import (
    MAPS_HOST_PATTERNS,
    UnsafeUrlError,
    stream_safe_url,
)

logger = logging.getLogger("MapsLink")

# Порядок важен: сначала самые надёжные формы.
#
# @lat,lng   — центр карты в /maps/place/...   (основная форма полной ссылки)
# !3dlat!4dlng — координаты самой точки, переживают смену центра карты
# q=/ll=/daddr= — координаты в параметре запроса
_PATTERNS = (
    re.compile(r'@(-?\d+\.\d+),(-?\d+\.\d+)'),
    re.compile(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)'),
    re.compile(r'[?&](?:q|ll|daddr|sll|center)=(-?\d+\.\d+),\s*(-?\d+\.\d+)'),
    # /maps/search/-8.626060,+115.102868 — короткая ссылка на точку без места
    # раскрывается именно в эту форму: координаты лежат в ПУТИ, а не в @ или
    # ?q= (поймано 08.08.2026 на The Sense; остальные ссылки Nuanu шли через @,
    # поэтому дыра всплыла не сразу). Пробел кодируется как '+' или %20.
    re.compile(r'/maps/(?:search|dir|place)/(-?\d+\.\d+),(?:\+|%20|\s)*(-?\d+\.\d+)'),
)

# Бали и окрестности: широта около -8, долгота около 115. Проверка нужна не
# ради географии, а чтобы поймать перевёрнутую пару: 115 в поле широты
# означает, что где-то по дороге координаты поменяли местами ещё раз.
_LAT_RANGE = (-90.0, 90.0)
_LNG_RANGE = (-180.0, 180.0)


def extract_coordinates(url: str) -> Optional[str]:
    """Достаёт координаты из ПОЛНОЙ ссылки и возвращает "долгота, широта".

    Возвращает None, если координат в ссылке нет (например, это короткая
    ссылка вида maps.app.goo.gl/... — её сперва надо раскрыть).
    """
    if not url or not isinstance(url, str):
        return None

    for pattern in _PATTERNS:
        m = pattern.search(url)
        if not m:
            continue
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
        except (TypeError, ValueError):
            continue
        if not (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1]):
            continue
        if not (_LNG_RANGE[0] <= lng <= _LNG_RANGE[1]):
            continue
        # В ссылке всегда "широта,долгота"; в поле карты — наоборот.
        return f"{lng}, {lat}"
    return None


def is_short_maps_link(url: str) -> bool:
    """Короткая ссылка, из которой координаты не достать без раскрытия."""
    if not url or not isinstance(url, str):
        return False
    return bool(re.search(r'(?:maps\.app\.goo\.gl|goo\.gl/maps)/', url))


async def resolve_maps_link(url: str, timeout: float = 15.0) -> Optional[str]:
    """Координаты по любой ссылке на карту, включая короткую.

    Короткая раскрывается через `stream_safe_url`: он идёт по редиректам и
    проверяет КАЖДЫЙ шаг (HTTPS, публичный IP, разрешённый хост), поэтому
    раскрытие чужой ссылки не превращается в дыру. Список хостов узкий —
    только домены карт.
    """
    if not url or not isinstance(url, str):
        return None

    direct = extract_coordinates(url)
    if direct:
        return direct

    try:
        async with stream_safe_url(
            url,
            timeout=timeout,
            allowed_hosts=MAPS_HOST_PATTERNS,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as response:
            final_url = str(response.url)
            coords = extract_coordinates(final_url)
            if coords:
                return coords
            # Google иногда отдаёт координаты не в адресе, а в теле страницы
            # (форма !3d/!4d в разметке) — читаем начало ответа.
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > 200_000:
                    break
            return extract_coordinates(body.decode("utf-8", "ignore"))
    except UnsafeUrlError as exc:
        logger.warning("Ссылка на карту отвергнута: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - сетевая ошибка не должна ронять разбор
        logger.warning("Не удалось раскрыть ссылку на карту: %s", exc)
        return None
