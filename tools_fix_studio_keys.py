"""
Правит Key существующих юнитов типа Studio, у которых в ключе застряло
'__0br' вместо '__studio'.

Причина: upsert_unit() (app/airtable_client.py) до исправления 05.08.2026
всегда подставляла в ключ f"{beds}br", а Bedrooms у Studio по определению
не заполняется -> запасное значение '0' уходило в ключ буквально. Код уже
исправлен (генерирует '__studio' для новых/обновляемых записей), этот
скрипт - разовый бэкфилл для 5 записей, найденных в живой базе 05.08.2026
до исправления.

Меняется ТОЛЬКО поле Key, только у записей, где Unit type = 'Studio' и
Key оканчивается на '__0br' - никаких других полей и типов юнитов скрипт
не трогает.

Запуск: python tools_fix_studio_keys.py            - только план
        python tools_fix_studio_keys.py --apply     - внести изменения
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, '.env'), override=True)

TOKEN = os.environ['AIRTABLE_TOKEN']
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv


def fetch(table):
    records, offset = [], None
    while True:
        params = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        url = f'https://api.airtable.com/v0/{BASE}/' + urllib.parse.quote(table) + '?' + urllib.parse.urlencode(params)
        data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20))
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
        urllib.request.urlopen(req, timeout=20)
        return True
    except urllib.error.HTTPError as e:
        print(f'      ОШИБКА {e.code}: {e.read().decode()[:180]}')
        return False


def main():
    units = fetch('Units')
    targets = [
        u for u in units
        if str(u['fields'].get('Unit type', '')).lower() == 'studio'
        and str(u['fields'].get('Key', '')).endswith('__0br')
    ]

    print(f"Найдено {len(targets)} записей Studio с ключом на '__0br':\n")

    changed = 0
    for u in targets:
        old_key = u['fields']['Key']
        new_key = old_key[: -len('__0br')] + '__studio'
        print(f"  {u['id']}  {old_key}  ->  {new_key}")
        if patch('Units', u['id'], {'Key': new_key}):
            changed += 1

    print()
    if APPLY:
        print(f"Применено: {changed}/{len(targets)} записей обновлено.")
    else:
        print(f"План: {len(targets)} записей будет изменено. Перезапустите с --apply, чтобы применить.")


if __name__ == '__main__':
    main()
