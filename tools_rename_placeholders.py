"""
Переименование записей Projects, у которых вместо названия заглушка.

Общее слово в имени ведет себя хуже, чем пустое поле: одинаковые заглушки
схлопываются между собой, и две разные виллы с разных концов острова
становятся одной записью. Уникальный номер это исключает.

Исходное значение не теряется: оно уходит в Aliases, чтобы при повторной
находке та же формулировка с баннера привела к этой же записи.

Запуск: python tools_rename_placeholders.py           — только план
        python tools_rename_placeholders.py --apply   — переименовать
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

from app.naming import is_generated_placeholder, is_placeholder_name, next_placeholder_name

TOKEN = os.environ['AIRTABLE_TOKEN']
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv

MERGED_PREFIX = '[merged]'


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


def guess_kind(fields) -> str:
    """Тип объекта из Property Type — он читается с баннера даже без названия."""
    types = fields.get('Property Type')
    if isinstance(types, (list, tuple)) and types:
        return str(types[0])
    if isinstance(types, str) and types.strip():
        return types.strip()
    return 'Project'


def main():
    projects = fetch('Projects')

    targets = []
    for rec in projects:
        name = (rec['fields'].get('Project Name') or '').strip()
        if not name or name.startswith(MERGED_PREFIX):
            continue
        if is_generated_placeholder(name):
            continue  # уже переименована
        if is_placeholder_name(name):
            targets.append(rec)

    if not targets:
        print('Записей с именами-заглушками не найдено.')
        return 0

    taken = [(r['fields'].get('Project Name') or '') for r in projects]

    mode = 'ПРИМЕНЯЮ ИЗМЕНЕНИЯ' if APPLY else 'ПЛАН (изменений не вносится)'
    print(f'=== {mode} ===')
    print(f'Записей к переименованию: {len(targets)}\n')

    for rec in targets:
        fields = rec['fields']
        old = (fields.get('Project Name') or '').strip()
        new = next_placeholder_name(guess_kind(fields), taken)
        taken.append(new)

        # Исходную формулировку сохраняем в Aliases: повторная находка с тем же
        # текстом на баннере должна привести сюда же, а не создать вторую запись.
        aliases = (fields.get('Aliases') or '').strip()
        parts = [a.strip() for a in aliases.split(',') if a.strip()] if aliases else []
        if old not in parts:
            parts.append(old)

        update = {'Project Name': new, 'Aliases': ', '.join(parts)}
        ok = patch('Projects', rec['id'], update)
        print(f"  {old!r}")
        print(f"      -> {new!r}   алиасы: {update['Aliases']!r}   {'ok' if ok else 'СБОЙ'}")

    if not APPLY:
        print('\nНичего не изменено. Для применения: python tools_rename_placeholders.py --apply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
