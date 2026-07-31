"""
Пересборка колонки Phones в таблице Agencies.

Первичная загрузка разбирала контакты регуляркой, которая допускала пробелы и
переносы строк внутри одного номера. Из-за этого пара вида
"62 823-4219-4697\n62 811-3999-335" склеивалась в одно число на 25 цифр.
Такой номер не совпадёт ни с чем и делает справочник бесполезным как раз там,
где у агентства записано несколько контактов.

Разбор берётся из app.dedup.extract_phones — того же кода, которым бот
сравнивает телефоны находок. Держать здесь вторую реализацию нельзя: она
разойдётся, и справочник снова начнёт врать.

Запуск: python tools_fix_agency_phones.py           — только план
        python tools_fix_agency_phones.py --apply   — перезаписать
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

from app.dedup import extract_phones

TOKEN = os.environ['AIRTABLE_TOKEN']
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv

SHEET_CSV = ('https://docs.google.com/spreadsheets/d/'
             '1c9x_5WwIO1Fwh4p8hqJmEbBeD94rzEih4m2DzsBrWIE/export?format=csv')

# Международный номер длиннее 15 цифр не бывает (E.164). Всё, что длиннее, —
# склейка нескольких номеров.
MAX_PHONE_DIGITS = 15


def fetch_agencies():
    records, offset = [], None
    while True:
        params = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        url = f'https://api.airtable.com/v0/{BASE}/Agencies?' + urllib.parse.urlencode(params)
        data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20))
        records += data['records']
        offset = data.get('offset')
        if not offset:
            return records


def load_source_contacts():
    """Исходные контакты из рабочей таблицы: {название агентства: сырая строка}."""
    with urllib.request.urlopen(SHEET_CSV, timeout=60) as resp:
        raw = resp.read().decode('utf-8')
    rows = list(csv.reader(io.StringIO(raw)))[1:]
    contacts = {}
    for row in rows:
        if len(row) < 4 or not row[1].strip():
            continue
        contacts[row[1].strip().lower()] = row[3] or ''
    return contacts


def patch(rec_id, fields):
    if not APPLY:
        return True
    url = f'https://api.airtable.com/v0/{BASE}/Agencies/{rec_id}'
    req = urllib.request.Request(url, data=json.dumps({'fields': fields}).encode(),
                                 headers=HEADERS, method='PATCH')
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except urllib.error.HTTPError as e:
        print(f'      ОШИБКА {e.code}: {e.read().decode()[:160]}')
        return False


def main():
    agencies = fetch_agencies()
    contacts = load_source_contacts()

    glued, changed = [], []
    for rec in agencies:
        name = (rec['fields'].get('Agency') or '').strip()
        current = rec['fields'].get('Phones') or ''
        correct = ', '.join(extract_phones(contacts.get(name.lower(), '')))

        if any(len(p.strip()) > MAX_PHONE_DIGITS for p in current.split(',') if p.strip()):
            glued.append((name, current, correct))
        elif current != correct:
            changed.append((name, current, correct))

    mode = 'ПРИМЕНЯЮ ИЗМЕНЕНИЯ' if APPLY else 'ПЛАН (изменений не вносится)'
    print(f'=== {mode} ===')
    print(f'агентств: {len(agencies)}   склеенных номеров: {len(glued)}   прочих расхождений: {len(changed)}\n')

    for name, current, correct in glued[:15]:
        print(f'  {name[:26]:28}')
        print(f'      было:  {current[:70]}')
        print(f'      стало: {correct[:70] or "(номеров не найдено)"}')
    if len(glued) > 15:
        print(f'  ... ещё {len(glued) - 15}\n')

    ok = 0
    for rec in agencies:
        name = (rec['fields'].get('Agency') or '').strip()
        current = rec['fields'].get('Phones') or ''
        correct = ', '.join(extract_phones(contacts.get(name.lower(), '')))
        if current != correct:
            if patch(rec['id'], {'Phones': correct}):
                ok += 1
            if APPLY:
                time.sleep(0.22)

    print(f'\nзаписей обновлено: {ok}')
    if not APPLY:
        print('Ничего не изменено. Для применения: python tools_fix_agency_phones.py --apply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
