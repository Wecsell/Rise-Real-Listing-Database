"""
Восстановление связи Projects -> Developer после переноса баз.

При переносе из Base RR New (Copy) в Base RR New Test все 193 проекта
доехали по имени, застройщики (139 записей) тоже доехали — но связь между
ними не проставилась: 149 из 150 проектов, у которых в источнике был
привязан застройщик, приехали в цель с пустым полем Developer.

Сопоставление — по точному совпадению имени застройщика (без учета регистра)
между источником и целью. Ничего не удаляется, трогаются только записи
Projects с уже пустым полем Developer.

Запуск: python tools_relink_developers.py           — только план
        python tools_relink_developers.py --apply   — записать связи
"""
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

TOKEN = os.environ['AIRTABLE_TOKEN']
TARGET_BASE = os.environ['AIRTABLE_BASE_ID']
SOURCE_BASE = os.environ.get('MIGRATION_SOURCE_BASE_ID', 'appwky2xeAYElrmYl')
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv


def fetch(base_id, table):
    records, offset = [], None
    while True:
        params = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        url = f'https://api.airtable.com/v0/{base_id}/' + urllib.parse.quote(table) + '?' + urllib.parse.urlencode(params)
        data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20))
        records += data['records']
        offset = data.get('offset')
        if not offset:
            return records


def patch(table, rec_id, fields):
    if not APPLY:
        return True
    url = f'https://api.airtable.com/v0/{TARGET_BASE}/' + urllib.parse.quote(table) + '/' + rec_id
    req = urllib.request.Request(url, data=json.dumps({'fields': fields}).encode(),
                                 headers=HEADERS, method='PATCH')
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except urllib.error.HTTPError as e:
        print(f'      ОШИБКА {e.code}: {e.read().decode()[:180]}')
        return False


def main():
    src_projects = fetch(SOURCE_BASE, 'Projects')
    src_devs = fetch(SOURCE_BASE, 'Developer')
    tgt_projects = fetch(TARGET_BASE, 'Projects')
    tgt_devs = fetch(TARGET_BASE, 'Developer')

    src_dev_name = {r['id']: (r['fields'].get('Developer') or '').strip() for r in src_devs}
    tgt_dev_by_name = {}
    tgt_dev_dupes = set()
    for r in tgt_devs:
        name = (r['fields'].get('Developer') or '').strip().lower()
        if not name:
            continue
        if name in tgt_dev_by_name:
            tgt_dev_dupes.add(name)
        tgt_dev_by_name[name] = r['id']

    tgt_proj_by_name = {}
    for r in tgt_projects:
        name = (r['fields'].get('Project Name') or '').strip().lower()
        if name and name not in tgt_proj_by_name:
            tgt_proj_by_name[name] = r

    to_relink, ambiguous, no_target_dev, already_linked, no_target_proj = [], [], [], [], []

    for sp in src_projects:
        src_dev_ids = sp['fields'].get('Developer') or []
        if not src_dev_ids:
            continue
        proj_name = (sp['fields'].get('Project Name') or '').strip()
        if not proj_name:
            continue

        tp = tgt_proj_by_name.get(proj_name.lower())
        if not tp:
            no_target_proj.append(proj_name)
            continue
        if tp['fields'].get('Developer'):
            already_linked.append(proj_name)
            continue

        dev_name = src_dev_name.get(src_dev_ids[0], '').strip()
        if not dev_name:
            continue
        dev_name_l = dev_name.lower()

        if dev_name_l in tgt_dev_dupes:
            ambiguous.append((proj_name, dev_name))
            continue

        tgt_dev_id = tgt_dev_by_name.get(dev_name_l)
        if not tgt_dev_id:
            no_target_dev.append((proj_name, dev_name))
            continue

        to_relink.append((tp['id'], proj_name, tgt_dev_id, dev_name))

    mode = 'ПРИМЕНЯЮ ИЗМЕНЕНИЯ' if APPLY else 'ПЛАН (изменений не вносится)'
    print(f'=== {mode} ===')
    print(f'проектов в источнике с застройщиком: {sum(1 for p in src_projects if p["fields"].get("Developer"))}')
    print(f'к восстановлению связи: {len(to_relink)}')
    print(f'уже привязаны в цели: {len(already_linked)}')
    print(f'неоднозначное имя застройщика (дубль в цели): {len(ambiguous)}')
    print(f'застройщик не найден в целевой базе: {len(no_target_dev)}')
    print(f'проект не найден в целевой базе: {len(no_target_proj)}')
    print()

    if ambiguous:
        print('НЕОДНОЗНАЧНЫЕ (пропущены, нужно решение вручную):')
        for proj, dev in ambiguous[:10]:
            print(f'   {proj[:34]:36} -> {dev}')
        print()

    if no_target_dev:
        print('ЗАСТРОЙЩИК НЕ НАЙДЕН В ЦЕЛИ (пропущены):')
        for proj, dev in no_target_dev[:10]:
            print(f'   {proj[:34]:36} -> {dev}')
        print()

    ok = 0
    for rec_id, proj_name, dev_id, dev_name in to_relink:
        success = patch('Projects', rec_id, {'Developer': [dev_id]})
        status = 'ok' if success else 'СБОЙ'
        print(f'  {proj_name[:40]:42} -> {dev_name[:28]:30} {status}')
        if success:
            ok += 1
        if APPLY:
            time.sleep(0.22)

    print(f'\nсвязей восстановлено: {ok}/{len(to_relink)}')
    if not APPLY:
        print('Ничего не изменено. Для применения: python tools_relink_developers.py --apply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
