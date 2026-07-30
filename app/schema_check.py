"""
Проверка расхождения между тем, что ожидает код, и живой схемой Airtable.

Зачем это нужно. Расхождение схемы — самый дорогой класс багов в проекте,
потому что он не падает, а тихо теряет данные:

- код писал русский Priority, а селект принимал только Hight/Medium/Low —
  запись падала целиком и помечалась Error;
- половина алиасов района вела на значения, которых в базе нет — район молча
  выбрасывался, и 33 проекта из 47 остались без него;
- промпт требовал 'Commercial' в Land Zoning Color, где база знает 'Brown';
- промпт перечислял типы Loft/Hotel/Hotel room, которых в Property Type нет.

Ни один из этих случаев не был виден в логах: ошибку глотал широкий except или
поле выбрасывалось до записи. Проверка делает расхождение громким.

Значения берутся из самого кода (AREA_ALIASES, AIRTABLE_PRIORITY, VALID_STAGES),
а не из отдельной копии списка — иначе мы просто завели бы третий список,
который тоже разойдется.

Запуск вручную:
    python -m app.schema_check
"""
import logging
import sys
from typing import List

from app.airtable_client import (
    AREA_ALIASES,
    UNIT_TYPE_ALIASES,
    VALID_POOL_VALUES,
    VALID_STAGES,
    field_exists,
    get_select_options,
)
from app.priority_parser import AIRTABLE_PRIORITY

logger = logging.getLogger("SchemaCheck")

# Поля, в которые код действительно пишет. Если поля нет в базе, Airtable
# отвергает весь апдейт записи с 422, а не игнорирует одно поле.
REQUIRED_FIELDS = {
    'Projects': [
        'Project Name', 'District', 'Location', 'Location Link',
        'Coordinates(for Map)', 'Property Type', 'Price From (USD)',
        'Price To (USD)', 'Construction stage', 'Distance to the beach, m2',
        'Handover Date', 'Ownership Type', 'Land Zoning Color',
        'Handover Permits', 'Link to Developer’s Kit (Rus)',
        'Link to Developer’s Kit (Eng)', 'Availability Chart', 'Developer',
        'Img', 'Source', 'Status', 'Gaps', 'Last updated', 'Active',
    ],
    'Units': [
        'Project Name', 'Unit type', 'Area', 'Area from (m²)',
        'Land Area (m²)', 'Price from(USD)', 'Bedrooms', 'Bathrooms',
        'Pool', 'Availability', 'Key', 'Img', 'Source', 'Status',
        'Gaps', 'Last updated',
    ],
    'Field Staging': [
        'Status', 'Developer', 'Project', 'Contact', 'Notes', 'Priority',
        'Coordinates', 'Photo', 'Audio', 'Google Maps Link',
        'Submitted By', 'Telegram Chat ID', 'Confirmed', 'Parsed JSON',
        'Possible Duplicate Of', 'Duplicate Reason',
    ],
    'Developer': [
        'Developer', 'Contacts', 'Language', 'Country of developer',
        'Notes', 'Listed By',
    ],
}


def expected_select_values() -> dict:
    """Значения селектов, которые код может отдать. Источник — сам код."""
    return {
        ('Projects', 'District'): set(AREA_ALIASES.values()),
        ('Projects', 'Construction stage'): set(VALID_STAGES),
        ('Field Staging', 'Priority'): set(AIRTABLE_PRIORITY.values()),
        ('Units', 'Pool'): set(VALID_POOL_VALUES),
        # None означает «поле не заполняем» — это не значение селекта
        ('Units', 'Unit type'): {v for v in UNIT_TYPE_ALIASES.values() if v},
    }


def check_schema_drift() -> List[str]:
    """
    Возвращает список расхождений. Пустой список — код и база согласованы.
    """
    problems: List[str] = []

    for table, fields in REQUIRED_FIELDS.items():
        for name in fields:
            if not field_exists(table, name):
                problems.append(
                    f"[поле отсутствует] {table}.{name!r} — код в него пишет, "
                    f"запись упадет с 422"
                )

    for (table, field), expected in expected_select_values().items():
        actual = set(get_select_options(table, field))
        if not actual:
            problems.append(
                f"[селект не прочитан] {table}.{field!r} — не удалось получить "
                f"список значений из базы"
            )
            continue
        missing = expected - actual
        if missing:
            problems.append(
                f"[значений нет в базе] {table}.{field!r}: {sorted(missing)} — "
                f"код их отдает, Airtable отвергнет"
            )

    return problems


def main() -> int:
    # Консоль Windows по умолчанию cp1251 и падает на 'm²' в именах полей
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    problems = check_schema_drift()
    if not problems:
        print("Схема согласована: расхождений между кодом и базой нет.")
        return 0

    print(f"Найдено расхождений: {len(problems)}\n")
    for p in problems:
        print(f"  {p}")
    print("\nОпции селектов Airtable через API не создаются и не удаляются — "
          "правьте в интерфейсе базы.")
    return 1


if __name__ == '__main__':
    sys.exit(main())
