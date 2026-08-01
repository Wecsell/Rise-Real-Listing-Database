"""
Разбор свалки в Projects."Extension Term (years)".

Поле текстовое, и за время жизни базы в него сложили всё подряд: сроки аренды
числом, тип владения, условия продления, прочерки и обрывки склеенных чисел.
Каждое значение уезжает туда, где его умеют читать:

  число 15..99      -> Lease Term (years)   (число; лет остаётся покупателю
                                             после сдачи — принятая договорённость)
  'Leasehold'       -> Ownership Type
  'Market'/'Guaranteed' -> Renewal Right    (вариант выбирается из живой схемы)
  '-' и '–'         -> очищается: информации в них нет

Что скрипт не трогает и выносит на глаза человеку: годы вместо сроков ('2030',
'2550'), склейки ('403030', '25+25+30'), значения вне диапазона ('5', '100'),
фразы целиком и уже занятые поля-приёмники. Гадать за человека дороже, чем
показать список.

Целевое поле очищается в том же PATCH, поэтому повторный запуск ничего не
находит и ничего не делает.

Запуск: python tools_fix_lease_terms.py           — только план
        python tools_fix_lease_terms.py --apply   — перенести
"""
import json
import os
import re
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
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv

TABLE = 'Projects'
SRC = 'Extension Term (years)'
LEASE = 'Lease Term (years)'
OWNERSHIP = 'Ownership Type'
RENEWAL = 'Renewal Right'

# Границы правдоподобного срока аренды на Бали. Ниже 15 — скорее срок рассрочки
# или продления, выше 99 — это уже год ('2030') или склейка ('403030').
MIN_YEARS, MAX_YEARS = 15, 99

# Выше этого срок формально проходит, но на Бали почти не встречается: три
# записи с 80 в Lease Term оказались суммой 25+25+30, а не сроком аренды.
SUSPICIOUS_YEARS = 60

# Значение -> поле-приёмник. Конкретный вариант селекта подбирается по живой
# схеме: захардкоженный список уже стоил проекту District на 33 записях.
TEXT_TARGETS = {
    'leasehold': OWNERSHIP,
    'market': RENEWAL,
    'guaranteed': RENEWAL,
}

# Прочерк любой длины и любого дефиса: '-', '–', '---'. Смысла не несёт.
DASHES = re.compile(r'^[-‐-―−]+$')

# Сколько строк каждого раздела печатать. Полные списки — в --verbose.
SAMPLE = 10
VERBOSE = '--verbose' in sys.argv


def get_schema():
    """Поля и варианты селектов прямо из базы, без запасных списков."""
    url = f'https://api.airtable.com/v0/meta/bases/{BASE}/tables'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {TOKEN}'})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    for table in data.get('tables', []):
        if table['name'] == TABLE:
            return {f['name']: f for f in table.get('fields', [])}
    raise SystemExit(f'Таблица {TABLE!r} не найдена в базе {BASE}')


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


def patch(table, rec_id, fields, attempts=3):
    """
    PATCH с повтором. Сеть до Airtable рвётся на длинных прогонах, а таймаут
    чтения прилетает мимо HTTPError и роняет весь запуск на середине.
    Повтор безопасен: PATCH идемпотентен, второй раз пишет то же самое.
    """
    if not APPLY:
        return True
    url = f'https://api.airtable.com/v0/{BASE}/' + urllib.parse.quote(table) + '/' + rec_id
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=json.dumps({'fields': fields}).encode(),
                                     headers=HEADERS, method='PATCH')
        try:
            urllib.request.urlopen(req, timeout=20)
            time.sleep(0.25)  # лимит Airtable — 5 запросов в секунду
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:180]
            if e.code == 429 and attempt < attempts:
                time.sleep(2 * attempt)
                continue
            print(f'      ОШИБКА {e.code}: {body}')
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < attempts:
                time.sleep(2 * attempt)
                continue
            print(f'      СЕТЬ: {e}')
            return False


def resolve_option(raw, options):
    """
    Вариант селекта по обрывку значения. Возвращает (вариант, кандидаты).

    'Guaranteed' однозначно указывает на 'Guaranteed at Market Price', а вот
    'Market' одинаково подходит и к 'Guaranteed at Market Price', и к
    'Priority at Market Price' — такие случаи уходят человеку, а не в базу.
    """
    low = raw.strip().lower()
    for opt in options:
        if opt.lower() == low:
            return opt, [opt]
    candidates = [o for o in options if low in o.lower()]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def is_empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or bool(DASHES.match(value.strip()))
    return False


def classify(records, renewal_options):
    """Раскладывает записи по действиям. Ничего не пишет."""
    moves, clears, review = [], [], []

    for rec in records:
        f = rec['fields']
        raw = f.get(SRC)
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.strip()
        name = (f.get('Project Name') or '?')[:30]
        row = {'rec': rec, 'name': name, 'raw': value}

        if DASHES.match(value):
            clears.append(row)
            continue

        if value.isdigit():
            years = int(value)
            if MIN_YEARS <= years <= MAX_YEARS:
                current = f.get(LEASE)
                if current not in (None, '') and float(current) != float(years):
                    review.append({**row, 'why': f'{LEASE} уже занято значением {current}'})
                    continue
                note = f' срок {years} лет выглядит завышенным' if years >= SUSPICIOUS_YEARS else ''
                moves.append({**row, 'field': LEASE, 'value': years, 'note': note})
            else:
                why = ('похоже на год, а не на срок' if 1900 <= years <= 2999
                       else f'вне диапазона {MIN_YEARS}..{MAX_YEARS}')
                review.append({**row, 'why': why})
            continue

        target = TEXT_TARGETS.get(value.lower())
        if target == OWNERSHIP:
            current = f.get(OWNERSHIP)
            if not is_empty(current):
                review.append({**row, 'why': f'{OWNERSHIP} уже занято: {current!r}'})
                continue
            moves.append({**row, 'field': OWNERSHIP, 'value': value, 'note': ''})
            continue

        if target == RENEWAL:
            option, candidates = resolve_option(value, renewal_options)
            if option is None:
                why = (f'{RENEWAL}: подходит несколько вариантов — {candidates}'
                       if candidates else f'{RENEWAL}: нет подходящего варианта в схеме')
                review.append({**row, 'why': why})
                continue
            current = f.get(RENEWAL)
            if current not in (None, '') and current != option:
                review.append({**row, 'why': f'{RENEWAL} уже занято: {current!r}'})
                continue
            moves.append({**row, 'field': RENEWAL, 'value': option, 'note': ''})
            continue

        review.append({**row, 'why': 'не число и не известное значение'})

    return moves, clears, review


def show(title, rows, render):
    if not rows:
        return
    print(f'\n{title}: {len(rows)}')
    shown = rows if VERBOSE else rows[:SAMPLE]
    for row in shown:
        print(render(row))
    if len(rows) > len(shown):
        print(f'    … ещё {len(rows) - len(shown)} (полный список: --verbose)')


def check_schema(schema):
    """Без этого запись улетит в 422 или молча не туда."""
    problems = []
    for field, expected in ((SRC, 'singleLineText'), (LEASE, 'number'),
                            (OWNERSHIP, None), (RENEWAL, 'singleSelect')):
        if field not in schema:
            problems.append(f'нет поля {field!r}')
        elif expected and schema[field]['type'] != expected:
            problems.append(f'{field!r}: тип {schema[field]["type"]}, ожидался {expected}')
    if problems:
        print('Схема базы не совпадает с ожидаемой:')
        for p in problems:
            print(f'  - {p}')
        raise SystemExit(1)


def verify(renewal_options):
    """Читает базу заново и проверяет, что переносить больше нечего."""
    moves, clears, review = classify(fetch(TABLE), renewal_options)
    print(f'\n=== ПРОВЕРКА ПОСЛЕ ЗАПИСИ ===')
    print(f'осталось к переносу: {len(moves)}   к очистке: {len(clears)}   '
          f'на разбор человеку: {len(review)}')
    if moves or clears:
        print('  ВНИМАНИЕ: часть изменений не применилась, см. ошибки выше.')
    else:
        print('  Повторный запуск ничего не изменит.')


def main():
    schema = get_schema()
    check_schema(schema)
    renewal_options = [c['name'] for c in schema[RENEWAL]['options']['choices']]

    records = fetch(TABLE)
    moves, clears, review = classify(records, renewal_options)

    mode = 'ПРИМЕНЯЮ ИЗМЕНЕНИЯ' if APPLY else 'ПЛАН (изменений не вносится)'
    filled = sum(1 for r in records if str(r['fields'].get(SRC) or '').strip())
    print(f'=== {mode} ===')
    print(f'Записей в {TABLE}: {len(records)}   заполнено {SRC}: {filled}')
    print(f'Варианты {RENEWAL} из схемы: {renewal_options}')

    by_field = {}
    for m in moves:
        by_field[m['field']] = by_field.get(m['field'], 0) + 1
    parts = '   '.join(f'-> {k}: {v}' for k, v in sorted(by_field.items()))
    print(f'\nперенос: {len(moves)}   {parts}')
    print(f'очистка прочерков: {len(clears)}   на разбор человеку: {len(review)}')

    show('ПЕРЕНОС', moves,
         lambda r: f"    {r['name']:32} {r['raw']!r:12} -> {r['field']} = {r['value']!r}{r['note']}")
    show('ОЧИСТКА', clears, lambda r: f"    {r['name']:32} {r['raw']!r}")
    show('НА РАЗБОР ЧЕЛОВЕКУ', review,
         lambda r: f"    {r['name']:32} {r['raw']!r:12} — {r['why']}")

    # Подозрительные сроки, уже лежащие в Lease Term: скрипт их не трогает,
    # но человеку на них смотреть стоит.
    odd = [(r['fields'].get('Project Name', '?')[:30], r['fields'][LEASE])
           for r in records
           if isinstance(r['fields'].get(LEASE), (int, float))
           and r['fields'][LEASE] >= SUSPICIOUS_YEARS]
    if odd:
        print(f'\nУЖЕ В {LEASE}, выглядит завышенным: {len(odd)}')
        for name, years in odd:
            print(f'    {name:32} {years}')

    todo = moves + [{**c, 'field': None} for c in clears]

    # Исходные значения есть только в Airtable, а очистка их затирает. Дамп
    # снимается до первого PATCH: без него откатывать будет нечего.
    if APPLY and todo:
        backup = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data',
                              f'backup_extension_term_{time.strftime("%Y%m%d_%H%M%S")}.json')
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        with open(backup, 'w', encoding='utf-8') as fh:
            json.dump([{'id': r['rec']['id'], 'name': r['name'], SRC: r['raw']}
                       for r in todo], fh, ensure_ascii=False, indent=2)
        print(f'\nИсходные значения сохранены: {backup}')

    ok_count = 0
    for row in todo:
        update = {SRC: ''}
        if row['field']:
            update[row['field']] = row['value']
        if patch(TABLE, row['rec']['id'], update):
            ok_count += 1

    if APPLY:
        print(f'\nЗаписано: {ok_count} из {len(moves) + len(clears)}')
        verify(renewal_options)
    else:
        print('\nНичего не изменено. Для применения: python tools_fix_lease_terms.py --apply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
