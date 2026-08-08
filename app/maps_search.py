# -*- coding: utf-8 -*-
"""
Поиск точки проекта по имени в Google Maps — с правом отказаться.

Зачем отдельно от `maps_link`: там координаты берутся из ссылки, которую дал
застройщик, и им можно верить. Здесь мы сами гадаем по названию, а на Бали
десятки вилл с одинаковыми именами (владелец, 08.08.2026: «если это не большой
проект, а локальная вилла, с вероятностью 90% найдётся что-то другое»).
Поэтому модуль устроен так, чтобы МОЛЧАТЬ, когда уверенности нет: пустой ответ
дешевле неверной точки на карте.

Три ворот, все обязательные:

1. **Карточка места, а не вьюпорт.** У выдачи поиска в адресе стоит
   `@<центр карты>,13z` — это центр экрана, а не объект: по «The Pavilions
   Nuanu Bali» он промахнулся на 10 км. Настоящие координаты лежат в
   `!3d<lat>!4d<lng>` карточки места либо в `@...` при зуме от 16.
2. **Имя найденного места должно сойтись** с именем проекта, а для короткого
   имени — ещё и с застройщиком. По «The Pavilions» рядом стоит «The Pavilions
   Bali» в Сануре, в 20 км от нужного; отличает их только слово OXO в названии
   верного места.
3. **Точка обязана попасть в границы района**, если район известен, иначе — в
   границы Бали.

Сеть модуль не трогает: выдачу отдаёт вызывающий (страницу карт отдаёт только
браузер, обычный fetch Google блокирует). Здесь — разбор и проверки, которые
можно прогнать тестами.
"""
import re
import unicodedata
from typing import Iterable, List, Optional, Tuple

# Границы Бали целиком — крайний рубеж, когда район неизвестен.
BALI_BOUNDS = (-9.20, -8.00, 114.40, 115.80)   # lat_min, lat_max, lng_min, lng_max

# Границы районов даны с запасом: задача — отсечь «другой конец острова»
# (Сануру от Чангу), а не выверить административную черту.
DISTRICT_BOUNDS = {
    'canggu':     (-8.680, -8.630, 115.115, 115.160),
    'berawa':     (-8.680, -8.640, 115.125, 115.160),
    'pererenan':  (-8.665, -8.615, 115.095, 115.135),
    'seseh':      (-8.660, -8.610, 115.075, 115.115),
    'cemagi':     (-8.665, -8.615, 115.065, 115.110),
    'nuanu':      (-8.650, -8.600, 115.080, 115.120),
    'kedungu':    (-8.640, -8.590, 115.045, 115.095),
    'tabanan':    (-8.680, -8.400, 114.950, 115.150),
    'umalas':     (-8.680, -8.640, 115.140, 115.175),
    'kerobokan':  (-8.690, -8.640, 115.145, 115.185),
    'seminyak':   (-8.710, -8.665, 115.150, 115.180),
    'kuta':       (-8.740, -8.690, 115.160, 115.195),
    'sanur':      (-8.720, -8.660, 115.245, 115.275),
    'ubud':       (-8.560, -8.470, 115.230, 115.290),
    'jimbaran':   (-8.810, -8.760, 115.150, 115.190),
    'ungasan':    (-8.855, -8.800, 115.125, 115.190),
    'uluwatu':    (-8.860, -8.790, 115.070, 115.160),
    'bukit':      (-8.870, -8.760, 115.070, 115.230),
    'nusa dua':   (-8.830, -8.770, 115.210, 115.250),
    'pandawa':    (-8.855, -8.810, 115.180, 115.220),
}

# Слова, которые ничего не различают: в названии виллы они есть у каждой второй.
_STOP = {
    'villa', 'villas', 'residence', 'residences', 'estate', 'estates', 'the',
    'by', 'bali', 'project', 'complex', 'apartment', 'apartments', 'hotel',
    'group', 'development', 'living', 'club', 'house', 'homes', 'suites',
}


def _norm(text: str) -> List[str]:
    text = unicodedata.normalize('NFKD', str(text or '')).lower()
    return [w for w in re.findall(r'[a-z0-9]+', text) if w]


def significant_tokens(name: str) -> List[str]:
    """Слова, по которым имя вообще можно узнать."""
    return [w for w in _norm(name) if w not in _STOP and len(w) > 2]


def is_risky_name(project_name: str) -> bool:
    """Имя, по которому поиск почти наверняка найдёт чужое.

    Одно значащее слово — это «Zen», «Solana», «Horizon»: таких вилл на Бали
    десятки. Для них нужен внешний признак (застройщик или район), само имя
    ничего не доказывает.
    """
    return len(significant_tokens(project_name)) <= 1


def parse_place(url: str, title: str = '') -> Optional[dict]:
    """Координаты из ссылки/карточки места. None — если это только вьюпорт."""
    if not url:
        return None

    name = ''
    m = re.search(r'/maps/place/([^/@]+)', url)
    if m:
        name = re.sub(r'\+', ' ', m.group(1))
        try:
            from urllib.parse import unquote
            name = unquote(name)
        except Exception:
            pass
    name = name or title or ''

    # Точка места — самый надёжный источник.
    m = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    if m:
        return {'name': name, 'lat': float(m.group(1)), 'lng': float(m.group(2)),
                'from': 'place'}

    # @-форма годится, только если зум «объектный». На 13z это центр экрана.
    m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+),(\d+(?:\.\d+)?)z', url)
    if m and float(m.group(3)) >= 16:
        return {'name': name, 'lat': float(m.group(1)), 'lng': float(m.group(2)),
                'from': 'zoomed-view'}
    return None


def in_bounds(lat: float, lng: float, district: Optional[str] = None) -> bool:
    key = (district or '').strip().lower()
    lo_lat, hi_lat, lo_lng, hi_lng = DISTRICT_BOUNDS.get(key, BALI_BOUNDS)
    return lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng


def name_matches(place_name: str, project_name: str) -> bool:
    """Все значащие слова проекта присутствуют в названии места."""
    want = set(significant_tokens(project_name))
    if not want:
        return False
    have = set(_norm(place_name))
    return want.issubset(have)


def developer_matches(place_name: str, developer: Optional[str]) -> bool:
    if not developer:
        return False
    want = set(significant_tokens(developer))
    if not want:
        return False
    return bool(want & set(_norm(place_name)))


def choose_place(candidates: Iterable[dict], project_name: str,
                 district: Optional[str] = None,
                 developer: Optional[str] = None) -> Optional[dict]:
    """Единственный подходящий кандидат — или None.

    Возвращает {'coordinates': "<lng>, <lat>", 'name', 'why'}; координаты сразу
    в порядке поля карты. Отказ — это нормальный ответ, а не ошибка.
    """
    good = []
    for c in candidates or []:
        place = parse_place(c.get('url', ''), c.get('name', '')) if 'lat' not in c else dict(c)
        if not place:
            continue
        place.setdefault('name', c.get('name', ''))
        if not name_matches(place['name'], project_name):
            continue
        if not in_bounds(place['lat'], place['lng'], district):
            continue
        dev_ok = developer_matches(place['name'], developer)
        # Короткое имя без внешнего подтверждения не принимаем: ни застройщика
        # в названии места, ни известного района — значит это может быть любая
        # из десятков одноимённых вилл.
        if is_risky_name(project_name) and not dev_ok and not district:
            continue
        place['dev_ok'] = dev_ok
        good.append(place)

    if not good:
        return None
    # Несколько разных точек прошли отбор — выбирать наугад нельзя.
    if len({(round(p['lat'], 4), round(p['lng'], 4)) for p in good}) > 1:
        want = _norm(project_name)
        # Точное совпадение имени решает спор: по «Bingin Elements» в выдаче
        # стоят и сам проект, и соседние «Elements A6 Villa Bingin» — все
        # содержат оба слова, но ровно одно место названо именно так.
        #
        # Но ТОЛЬКО для отличимого имени. У рискованного (одно значащее слово)
        # точное совпадение ничего не доказывает: «Serenity Villas» есть у
        # PT Global BALI HOME, у ADVA и ещё у двух застройщиков — совпадение
        # букв не говорит, чей это объект. Там спор решает только застройщик.
        exact = ([p for p in good if _norm(p['name']) == want]
                 if not is_risky_name(project_name) else [])
        best = exact if len(exact) == 1 else [p for p in good if p['dev_ok']]
        if len(best) != 1:
            return None
        good = best

    p = good[0]
    why = f"место «{p['name']}»"
    why += ', застройщик совпал' if p['dev_ok'] else ''
    why += f", в границах {district}" if district else ', в границах Бали'
    return {'coordinates': f"{p['lng']}, {p['lat']}", 'name': p['name'],
            'source': p['from'], 'why': why}
