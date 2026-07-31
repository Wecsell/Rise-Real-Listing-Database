"""
Сверка таблицы Agencies с рабочей гугл-таблицей партнёров.

Делает три вещи:

1. Приводит телефоны к виду "+62 8133919882". Голая строка цифр читается как
   абракадабра — непонятно, где кончается код страны.
2. Переносит сайты и аккаунты (Telegram, Instagram) отдельными колонками.
   Они тоже должны участвовать в поиске дублей, причём совпадение сайта —
   стопроцентный признак того же застройщика: собственный домен не бывает
   общим, в отличие от телефона, который может принадлежать агенту.
3. Показывает расхождения с источником, чтобы данные можно было перепроверить.

Разбор берётся из app.dedup и app.phone_formatter — тем же кодом бот сравнивает
контакты находок. Вторая реализация здесь недопустима: она разойдётся, и
справочник начнёт врать.

Запуск: python tools_sync_agencies.py           — только план
        python tools_sync_agencies.py --apply   — записать
"""
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

from app.dedup import extract_phones, extract_social_handles, extract_websites
from app.phone_formatter import format_international

TOKEN = os.environ['AIRTABLE_TOKEN']
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv

SHEET_CSV = ('https://docs.google.com/spreadsheets/d/'
             '1c9x_5WwIO1Fwh4p8hqJmEbBeD94rzEih4m2DzsBrWIE/export?format=csv')

NEW_FIELDS = [
    ('Websites', 'multilineText', 'Домены компании. Совпадение — стопроцентный дубль.'),
    ('Handles', 'multilineText', 'Аккаунты в Telegram и Instagram в виде tg:name / ig:name.'),
]


def api(path, data=None, method='GET'):
    url = f'https://api.airtable.com/v0/{path}'
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    return json.load(urllib.request.urlopen(req))


def meta(path, data=None, method='GET'):
    url = f'https://api.airtable.com/v0/meta/bases/{BASE}/{path}'
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    return json.load(urllib.request.urlopen(req))


def ensure_fields():
    tables = meta('tables')['tables']
    agencies = [t for t in tables if t['name'] == 'Agencies'][0]
    present = {f['name'] for f in agencies['fields']}
    for name, ftype, description in NEW_FIELDS:
        if name in present:
            print(f'  поле {name}: уже есть')
            continue
        if not APPLY:
            print(f'  поле {name}: будет создано')
            continue
        try:
            meta(f"tables/{agencies['id']}/fields",
                 {'name': name, 'type': ftype, 'description': description}, 'POST')
            print(f'  поле {name}: СОЗДАНО')
        except urllib.error.HTTPError as e:
            print(f'  поле {name}: ОШИБКА {e.code} {e.read().decode()[:140]}')


def fetch_agencies():
    records, offset = [], None
    while True:
        params = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        data = api(f'{BASE}/Agencies?' + urllib.parse.urlencode(params))
        records += data['records']
        offset = data.get('offset')
        if not offset:
            return records


def load_source():
    """{название: (контакты, сайт/email)} из рабочей таблицы."""
    with urllib.request.urlopen(SHEET_CSV, timeout=60) as resp:
        raw = resp.read().decode('utf-8')
    rows = list(csv.reader(io.StringIO(raw)))[1:]
    source = {}
    for row in rows:
        if len(row) < 5 or not row[1].strip():
            continue
        source[row[1].strip().lower()] = (row[3] or '', row[4] or '')
    return source


def main():
    print('=== поля ===')
    ensure_fields()

    agencies = fetch_agencies()
    source = load_source()

    print(f'\n=== сверка с гугл-таблицей ({len(agencies)} агентств) ===')
    missing_in_sheet, updates = [], []
    stats = {'phones': 0, 'websites': 0, 'handles': 0}

    for rec in agencies:
        name = (rec['fields'].get('Agency') or '').strip()
        if name.lower() not in source:
            missing_in_sheet.append(name)
            continue

        contacts, site_col = source[name.lower()]
        blob = f'{contacts}\n{site_col}'

        phones = [format_international(p) for p in extract_phones(blob)]
        websites = extract_websites(site_col)
        handles = extract_social_handles(blob)

        wanted = {
            'Phones': ', '.join(p for p in phones if p),
            'Websites': ', '.join(websites),
            'Handles': ', '.join(handles),
        }
        current = {k: (rec['fields'].get(k) or '') for k in wanted}
        if current != wanted:
            updates.append((rec['id'], name, current, wanted))

        stats['phones'] += len(phones)
        stats['websites'] += len(websites)
        stats['handles'] += len(handles)

    print(f"телефонов: {stats['phones']}   сайтов: {stats['websites']}   аккаунтов: {stats['handles']}")
    print(f"записей к обновлению: {len(updates)}")
    if missing_in_sheet:
        print(f"нет в гугл-таблице: {len(missing_in_sheet)} -> {missing_in_sheet[:5]}")

    print('\n=== примеры ===')
    for _rid, name, current, wanted in updates[:6]:
        print(f'  {name[:26]:28}')
        print(f"      было:  Phones={current['Phones'][:44]!r}")
        print(f"      стало: Phones={wanted['Phones'][:44]!r}")
        if wanted['Websites']:
            print(f"             Websites={wanted['Websites'][:50]!r}")
        if wanted['Handles']:
            print(f"             Handles={wanted['Handles'][:50]!r}")

    if not APPLY:
        print('\nНичего не изменено. Для применения: python tools_sync_agencies.py --apply')
        return 0

    ok = 0
    for rec_id, _name, _current, wanted in updates:
        try:
            api(f'{BASE}/Agencies/{rec_id}', {'fields': wanted}, 'PATCH')
            ok += 1
            time.sleep(0.22)
        except urllib.error.HTTPError as e:
            print(f'  ОШИБКА {e.code}: {e.read().decode()[:140]}')
    print(f'\nобновлено записей: {ok}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
