"""
Разворот порядка координат в Projects.Coordinates(for Map).

Карта Airtable читает пару как "долгота, широта", а полевой бот всё это время
писал "широта, долгота" — так их отдаёт Telegram. Из-за этого точки на карте
вставали не туда.

Ссылки Location Link и Google Maps Link не трогаются: Google ждёт как раз
"широта,долгота", и там всё было правильно.

Порядок определяется по значению, а не по позиции: широта Бали лежит около
-8, долгота около 115. Уже развёрнутые записи пропускаются, поэтому повторный
запуск ничего не испортит.

Запуск: python tools_fix_coordinates.py           — только план
        python tools_fix_coordinates.py --apply   — развернуть
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

from app.naming import swap_coordinates

TOKEN = os.environ['AIRTABLE_TOKEN']
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv

FIELD = 'Coordinates(for Map)'

# Границы Индонезии с запасом. Нужны, чтобы определить порядок по значению,
# а не гадать по позиции.
LAT_RANGE = (-11.0, 6.0)
LNG_RANGE = (94.0, 142.0)


def fetch(table):
    records, offset = [], None
    while True:
        params = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        url = f'https://api.airtable.com/v0/{BASE}/' + urllib.parse.quote(table) + '?' + urllib.parse.urlencode(params)
        data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS)))
        records += data['records']
        offset = data.get('offset')
        if not offset:
            return records


def patch(table, rec_id, fields):
    if not APPLY:
        return True
    url = f'https://api.airtable.com/v0/{BASE}/' + urllib.parse.quote(table) + '/' + rec_id
    req = urllib.request.Request(url, data=json.dumps({'fields': fields}).encode(),
                                 headers=HEADERS, method='PATCH')
    try:
        urllib.request.urlopen(req)
        return True
    except urllib.error.HTTPError as e:
        print(f'      ОШИБКА {e.code}: {e.read().decode()[:180]}')
        return False


def detect_order(raw):
    """'lat,lng', 'lng,lat' или None, если разобрать не удалось."""
    if not raw or not isinstance(raw, str):
        return None
    parts = [p.strip() for p in raw.split(',')]
    if len(parts) != 2:
        return None
    try:
        first, second = float(parts[0]), float(parts[1])
    except ValueError:
        return None

    first_is_lat = LAT_RANGE[0] <= first <= LAT_RANGE[1]
    second_is_lng = LNG_RANGE[0] <= second <= LNG_RANGE[1]
    if first_is_lat and second_is_lng:
        return 'lat,lng'

    first_is_lng = LNG_RANGE[0] <= first <= LNG_RANGE[1]
    second_is_lat = LAT_RANGE[0] <= second <= LAT_RANGE[1]
    if first_is_lng and second_is_lat:
        return 'lng,lat'
    return None


def main():
    projects = fetch('Projects')

    to_fix, already, unclear = [], [], []
    for rec in projects:
        raw = rec['fields'].get(FIELD)
        if not raw:
            continue
        order = detect_order(raw)
        if order == 'lat,lng':
            to_fix.append(rec)
        elif order == 'lng,lat':
            already.append(rec)
        else:
            unclear.append(rec)

    mode = 'ПРИМЕНЯЮ ИЗМЕНЕНИЯ' if APPLY else 'ПЛАН (изменений не вносится)'
    print(f'=== {mode} ===')
    print(f'к развороту: {len(to_fix)}   уже верных: {len(already)}   непонятных: {len(unclear)}\n')

    for rec in unclear:
        print(f"  ПРОПУСК (не разобрано): {rec['fields'].get(FIELD)!r}")

    for rec in to_fix:
        old = rec['fields'][FIELD]
        new = swap_coordinates(old)
        name = rec['fields'].get('Project Name', '?')
        ok = patch('Projects', rec['id'], {FIELD: new})
        print(f"  {name[:34]:36} {old:26} -> {new:26} {'ok' if ok else 'СБОЙ'}")

    if not APPLY:
        print('\nНичего не изменено. Для применения: python tools_fix_coordinates.py --apply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
