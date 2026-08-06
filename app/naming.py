"""
Имена-заглушки: распознавание и генерация.

Проблема, которую это решает, видна на живых данных. Когда модель не смогла
определить название, в базу уезжало общее слово: 'VILLA', 'Unknown Project',
'DESIGNER VILLA', '1-Bed Villa with Pool'. Такие имена ведут себя хуже, чем
отсутствие имени, сразу с двух сторон:

- одинаковые заглушки схлопываются между собой: две разные виллы с именем
  'VILLA' считаются одним проектом;
- заглушка 'Unknown' у застройщика делала обратное — привязывала проект к
  фиктивной записи, из-за чего при появлении настоящего застройщика строгая
  иерархия отказывалась от совпадения и создавала дубль проекта.

Решение: заглушка получает уникальный номер ('Unknown Villa 7'), и заглушки
никогда не сопоставляются друг с другом. Когда настоящее название всплывет в
материалах, запись переименовывается — сопоставление к этому моменту идет уже
по телефону застройщика, а не по имени.
"""
import re
from typing import Iterable, Optional

PLACEHOLDER_PREFIX = 'Unknown'

# Слова, которые сами по себе названием не являются
_GENERIC_WORDS = {
    # тип объекта
    'villa', 'villas', 'apartment', 'apartments', 'studio', 'townhouse',
    'project', 'projects', 'complex', 'development', 'property', 'unit',
    'house', 'land', 'resort', 'area',
    'unknown', 'unnamed', 'none', 'n', 'a', 'na', 'tbd',
    'вилла', 'виллы', 'апартаменты', 'проект', 'студия', 'неизвестно', 'дом',
    # предлоги и артикли
    'in', 'at', 'on', 'the', 'of', 'for', 'with', 'and', 'by', 'to',
    'в', 'на', 'и', 'с', 'от',
}

# Районы — слабый признак. Сами по себе названием быть могут: застройщик
# CEMAGI существует, у него есть контакты и сайт cemagirock.com. Пустым имя
# делает только СВЯЗКА типа объекта с районом: 'Villa in Canggu', 'CEMAGI
# House'. Поэтому район засчитывается в мусор лишь тогда, когда в имени уже
# есть слово, обозначающее тип.
_DISTRICT_WORDS = {
    'canggu', 'seminyak', 'kuta', 'ubud', 'uluwatu', 'ungasan', 'jimbaran',
    'sanur', 'denpasar', 'cemagi', 'seseh', 'pererenan', 'umalas', 'kerobokan',
    'nuanu', 'kedungu', 'tabanan', 'bukit', 'bingin', 'berawa', 'bali',
    'чангу', 'семиньяк', 'кута', 'убуд', 'улувату', 'санур', 'бали',
}

# Слова, обозначающие тип объекта — «сильный» признак пустого имени
_TYPE_WORDS = {
    'villa', 'villas', 'apartment', 'apartments', 'studio', 'townhouse',
    'house', 'land', 'project', 'projects', 'complex', 'property', 'unit',
    'вилла', 'виллы', 'апартаменты', 'проект', 'студия', 'дом',
}

# Структурные обороты: они описывают тип сделки/листинга, а не название,
# поэтому ловятся безусловно, где бы ни встретились в строке.
_MARKETING_MARKERS = (
    'for sale', 'for lease', 'for rent', 'leasehold', 'freehold',
    'brand new', 'best ',
    'over contract', 'local developer', 'property by',
)

# Декоративные прилагательные — не безусловный маркер. В рекламе Бали они
# сплошь и рядом стоят РЯДОМ с настоящим именем ('Amali Luxury Residence',
# 'Beraban Luxury Lofts', 'Exclusive Villas Collection'), и безусловный запрет
# на них ложно превращал настоящие названия в заглушки. Слово вычитается как
# мусорное — так же, как 'villa' или 'project' — а не убивает всю строку.
_DECORATIVE_ADJECTIVES = {'luxury', 'designer', 'exclusive'}

# Описание юнита вместо названия проекта: '1-Bed Villa with Pool', '4-Bedroom Villa'
_UNIT_DESCRIPTION = re.compile(
    r'^\s*\d+\s*[- ]?\s*(?:bed|bedroom|br|спальн)', re.I
)

_PLACEHOLDER_WITH_NUMBER = re.compile(
    rf'^{PLACEHOLDER_PREFIX}\s*[\w\s]*?\s*(\d+)\s*$', re.I
)


def is_placeholder_name(name: Optional[str]) -> bool:
    """
    Является ли строка заглушкой, а не настоящим названием.

    Ловит четыре случая: пусто, общее слово ('VILLA', 'Unknown Project'),
    рекламный оборот ('Luxury Villas For Sale') и описание юнита
    ('1-Bed Villa with Pool').
    """
    if not name or not isinstance(name, str):
        return True

    text = name.strip()
    if not text:
        return True

    lowered = text.lower()

    if _UNIT_DESCRIPTION.match(lowered):
        return True

    if any(marker in lowered for marker in _MARKETING_MARKERS):
        return True

    # Убираем знаки препинания и смотрим, остались ли содержательные слова
    words = re.findall(r'[\w]+', lowered)
    if not words:
        return True

    junk = set(_GENERIC_WORDS) | _DECORATIVE_ADJECTIVES
    if any(w in _TYPE_WORDS for w in words):
        junk |= _DISTRICT_WORDS

    meaningful = [w for w in words if w not in junk and not w.isdigit()]
    return not meaningful


def is_generated_placeholder(name: Optional[str]) -> bool:
    """Имя, которое сгенерировали мы сами: 'Unknown Villa 7'."""
    if not name or not isinstance(name, str):
        return False
    return bool(_PLACEHOLDER_WITH_NUMBER.match(name.strip()))


def _used_numbers(existing_names: Iterable[str]) -> set:
    numbers = set()
    for n in existing_names or []:
        match = _PLACEHOLDER_WITH_NUMBER.match(str(n).strip())
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def next_placeholder_name(kind: Optional[str], existing_names: Iterable[str]) -> str:
    """
    Следующее свободное имя-заглушка: 'Unknown Villa 7', для застройщика —
    просто 'Unknown 2'.

    Номер сквозной по всем видам, а не отдельный для вилл и апартаментов:
    так он остается уникальным идентификатором находки, даже если тип потом
    уточнят. Уникальность обязательна — одинаковые заглушки схлопываются
    между собой и превращают разные объекты в один.
    """
    used = _used_numbers(existing_names)
    number = 1
    while number in used:
        number += 1

    kind_clean = str(kind).strip() if kind else ''
    if kind_clean:
        return f"{PLACEHOLDER_PREFIX} {kind_clean} {number}"
    return f"{PLACEHOLDER_PREFIX} {number}"


def swap_coordinates(coords: Optional[str]) -> Optional[str]:
    """
    Меняет порядок пары координат местами.

    Два потребителя ждут разного, и это не ошибка, а два разных стандарта:

    - Google Maps в ссылке `?q=` ждет "широта,долгота" — так их отдает Telegram
      и так они приходят от полевого бота;
    - поле Projects."Coordinates(for Map)" хранит "долгота,широта", то есть для
      Бали значение начинается с 115 (канон владельца, подтверждён 06.08.2026).

    Поэтому внутри держим порядок Telegram, а переворачиваем только на записи
    в поле карты. Раньше в оба места писалось одно и то же значение.

    Карта на странице Interfaces читает Coordinates(for Map) как есть, в этом
    самом порядке, и точки встают верно - проверено в живом интерфейсе
    06.08.2026. Никакого второго поля под карту не нужно.

    Если карта пуста - она, скорее всего, ещё геокодирует: у неё есть кэш, и
    после его сброса полный проход по всем записям занимает минуты. 06.08.2026
    я принял это за ошибку порядка координат и семь раз переставил значения в
    живой базе, прежде чем открыть настройки карты и увидеть, что канон верен.
    Сначала смотреть настройки и дождаться загрузки, потом трогать данные.
    """
    if not coords or not isinstance(coords, str):
        return coords

    parts = [p.strip() for p in coords.split(',')]
    if len(parts) != 2:
        return coords

    try:
        float(parts[0])
        float(parts[1])
    except ValueError:
        return coords

    return f"{parts[1]}, {parts[0]}"


def placeholders_never_match(name_a: Optional[str], name_b: Optional[str]) -> bool:
    """
    Запрещено ли сопоставлять эти два имени.

    Две заглушки — это две разные находки, о которых мы просто ничего не знаем.
    Считать их одним проектом нельзя: именно так 'VILLA' и 'VILLA' с разных
    концов острова оказывались одной записью.
    """
    return is_placeholder_name(name_a) and is_placeholder_name(name_b)
