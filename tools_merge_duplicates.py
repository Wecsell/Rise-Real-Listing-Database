"""
Слияние точных дублей в Projects.

Правило (подтверждено владельцем): оставляем запись с настоящим застройщиком;
если оба застройщика — заглушки, оставляем ту, где больше заполненных полей.
Юниты и находки переносятся на оставляемую запись, недостающие поля
дозаполняются.

Проигравшая запись НЕ удаляется: удаление через API необратимо. Она
переименовывается с пометкой слияния, чтобы перестать участвовать в
сопоставлении по имени, и остаётся видимой для ручного удаления.

Запуск: python merge_dupes.py            — только план
        python merge_dupes.py --apply    — внести изменения
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

PROJECT_DIR = r'D:\Antigravity\Bots RV'
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, '.env'), override=True)

from app.naming import is_placeholder_name

TOKEN = os.environ['AIRTABLE_TOKEN']
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv

MERGED_PREFIX = '[merged]'
SKIP_ON_MERGE = {'Project Name', 'Units', 'Field Staging', 'Developer',
                 'Last updated', 'Unit ID', 'Map Cash'}


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


def filled_count(record):
    return sum(1 for v in record['fields'].values() if v not in (None, '', [], {}))


def main():
    projects = fetch('Projects')
    units = fetch('Units')
    staging = fetch('Field Staging')
    dev_names = {d['id']: (d['fields'].get('Developer') or '') for d in fetch('Developer')}

    def has_real_developer(record):
        links = record['fields'].get('Developer') or []
        return any(not is_placeholder_name(dev_names.get(d, '')) for d in links)

    groups = defaultdict(list)
    for p in projects:
        name = (p['fields'].get('Project Name') or '').strip()
        if name and not name.startswith(MERGED_PREFIX):
            groups[name.lower()].append(p)

    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    if not duplicates:
        print('Точных дублей не найдено.')
        return 0

    mode = 'ПРИМЕНЯЮ ИЗМЕНЕНИЯ' if APPLY else 'ПЛАН (изменений не вносится)'
    print(f'=== {mode} ===\n')

    for name, group in duplicates.items():
        keeper = sorted(group, key=lambda r: (has_real_developer(r), filled_count(r)), reverse=True)[0]
        losers = [r for r in group if r['id'] != keeper['id']]

        k_devs = [dev_names.get(d) for d in (keeper['fields'].get('Developer') or [])]
        print(f"{keeper['fields'].get('Project Name')}")
        print(f"  оставляю  {keeper['id']}  застройщик={k_devs}  полей={filled_count(keeper)}")

        for loser in losers:
            l_devs = [dev_names.get(d) for d in (loser['fields'].get('Developer') or [])]
            print(f"  сливаю    {loser['id']}  застройщик={l_devs}  полей={filled_count(loser)}")

            moved_units = [u for u in units if loser['id'] in (u['fields'].get('Project Name') or [])]
            for unit in moved_units:
                links = [x for x in unit['fields']['Project Name'] if x != loser['id']]
                if keeper['id'] not in links:
                    links.append(keeper['id'])
                ok = patch('Units', unit['id'], {'Project Name': links})
                print(f"      юнит {unit['id']} -> оставляемой  {'ok' if ok else 'СБОЙ'}")

            moved_staging = [s for s in staging if loser['id'] in (s['fields'].get('Project') or [])]
            for rec in moved_staging:
                links = [x for x in rec['fields']['Project'] if x != loser['id']]
                if keeper['id'] not in links:
                    links.append(keeper['id'])
                ok = patch('Field Staging', rec['id'], {'Project': links})
                print(f"      находка {rec['id']} -> оставляемой  {'ok' if ok else 'СБОЙ'}")

            gained = {k: v for k, v in loser['fields'].items()
                      if k not in SKIP_ON_MERGE
                      and v not in (None, '', [], {})
                      and keeper['fields'].get(k) in (None, '', [], {})}
            if gained:
                ok = patch('Projects', keeper['id'], gained)
                print(f"      дозаполнено полей: {list(gained)}  {'ok' if ok else 'СБОЙ'}")

            marked = f"{MERGED_PREFIX} {loser['fields'].get('Project Name')} -> {keeper['id']}"
            ok = patch('Projects', loser['id'], {'Project Name': marked, 'Active': False})
            print(f"      помечена как слитая: {marked!r}  {'ok' if ok else 'СБОЙ'}")
        print()

    if not APPLY:
        print('Ничего не изменено. Для применения: python merge_dupes.py --apply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
