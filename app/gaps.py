"""
Расчет незаполненных полей (Gaps) в коде, а не со слов модели.

Промпт просит Gemini самому перечислить, что он не нашел. На это нельзя
опираться: модель перечисляет пропуски непоследовательно, а когда данных мало —
склонна возвращать пустой список, потому что «ответ выглядит законченным».
Между тем именно Gaps решают, поедет ли карточка в статус Needs data и о чем
мы потом спросим застройщика.

Здесь список считается вычитанием: что из обязательного реально заполнено.
"""
from typing import Any, Dict, List, Optional

# Поле -> человекочитаемое имя для сообщения застройщику.
# Порядок важен: в таком виде пропуски попадут в Airtable и в текст запроса.
REQUIRED_PROJECT_FIELDS = {
    'Project Name': 'название проекта',
    'District': 'район',
    'Property Type': 'тип недвижимости',
    'Price From (USD)': 'цена от (USD)',
    'Construction stage': 'стадия строительства',
    'Handover Date': 'дата сдачи',
    'Ownership Type': 'форма собственности',
    'Land Zoning Color': 'зонирование земли',
    'Handover Permits': 'разрешение на строительство (PBG/SLF)',
    'Link to Dev Kit (Rus)': 'презентация проекта',
    'Availability Chart': 'шахматка',
}

REQUIRED_UNIT_FIELDS = {
    'Unit type': 'тип юнита',
    'Bedrooms': 'количество спален',
    'Area from (m2)': 'площадь',
    'Price from(USD)': 'цена юнита (USD)',
    'Availability': 'наличие',
}

REQUIRED_DEVELOPER_FIELDS = {
    'Developer': 'название застройщика',
    'Contacts': 'контакты',
}


def is_filled(value: Any) -> bool:
    """
    Значение считается заполненным, если оно осмысленно.

    Airtable и Gemini по-разному отдают «пусто»: None, пустая строка, пробелы,
    пустой список, а модель иногда пишет строку 'None' или 'N/A'.
    """
    if value is None:
        return False
    if isinstance(value, str):
        cleaned = value.strip().lower()
        return cleaned not in ('', 'none', 'n/a', 'na', '-', 'нет данных', 'unknown')
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


# Одно и то же поле называется по-разному у парсера и в Airtable: имена слева
# описаны в контракте gemini_parser, имена справа airtable_client подставляет
# при записи (см. его же переименования 'Link to Dev Kit (Rus)' и
# 'Area from (m2)'). Пропуски считаются по записи, ПРОЧИТАННОЙ ИЗ БАЗЫ, то есть
# по правым именам, а список обязательных полей знал только левые — и эти два
# пропуска не закрывались ничем. Каждый проект вечно требовал презентацию,
# каждый юнит — площадь, и оба уезжали в вопросы застройщику выдуманными.
FIELD_ALIASES = {
    'Link to Dev Kit (Rus)': ('Link to Developer’s Kit (Rus)',),
    'Link to Dev Kit (Eng)': ('Link to Developer’s Kit (Eng)',),
    'Area from (m2)': ('Area from (m\xb2)',),
    'Land Area (m2)': ('Land Area (m\xb2)',),
}


def _any_filled(fields: Dict[str, Any], key: str) -> bool:
    """Заполнено ли поле под любым из своих имён."""
    return any(is_filled(fields.get(name))
               for name in (key, *FIELD_ALIASES.get(key, ())))


def compute_gaps(fields: Optional[Dict[str, Any]], required: Dict[str, str]) -> List[str]:
    """Человекочитаемые имена обязательных полей, которых не хватает."""
    fields = fields or {}
    return [label for key, label in required.items() if not _any_filled(fields, key)]


def project_gaps(fields: Optional[Dict[str, Any]]) -> List[str]:
    """
    Пропуски по проекту с учетом условных правил.

    Срок аренды спрашиваем только у leasehold: у freehold его не существует,
    и попадание такого пункта в запрос застройщику выглядит некомпетентно.
    """
    gaps = compute_gaps(fields, REQUIRED_PROJECT_FIELDS)

    fields = fields or {}
    ownership = str(fields.get('Ownership Type') or '').strip().lower()
    if 'leasehold' in ownership and not is_filled(fields.get('Lease Term (years)')):
        gaps.append('срок аренды (лет)')

    return gaps


def unit_gaps(fields: Optional[Dict[str, Any]]) -> List[str]:
    return compute_gaps(fields, REQUIRED_UNIT_FIELDS)


def developer_gaps(fields: Optional[Dict[str, Any]]) -> List[str]:
    return compute_gaps(fields, REQUIRED_DEVELOPER_FIELDS)


def merge_gaps(*sources) -> List[str]:
    """
    Объединяет пропуски из разных источников, сохраняя порядок и убирая повторы.

    Источников три: вычисленные здесь, присланные моделью в поле Gaps и
    добавленные при записи в Airtable (например нераспознанный район).
    """
    seen = set()
    merged = []
    for source in sources:
        if not source:
            continue
        if isinstance(source, str):
            source = [part.strip() for part in source.split(',')]
        for item in source:
            if not item:
                continue
            item = str(item).strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                merged.append(item)
    return merged
