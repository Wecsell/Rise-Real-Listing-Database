"""
Решения о судьбе записи в Field Staging.

Вынесено отдельным модулем, потому что раньше вся логика жила внутри одного
длинного цикла с сетевыми вызовами и проверить ее без Airtable и Gemini было
нельзя. Здесь только чистые функции над словарем записи.

Две вещи, которые они закрывают:

1. Находка из поля больше не уезжает в основную базу автоматически. Фото
   баннера и голосовая заметка — это гипотеза, а не проверенные данные.
   Перенос происходит только после отметки Confirmed (или, позже, после
   ответа застройщика).

2. Повторная обработка не создает дублей. Раньше field_processor писал в
   Developer/Projects/Units и только потом помечал запись Processed. Падение
   между этими шагами — сеть, лимит Airtable, любая ошибка разбора — означало,
   что на следующем тике через 30 секунд все создастся заново.
"""
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("Staging")

PARSED_JSON_FIELD = 'Parsed JSON'
CONFIRMED_FIELD = 'Confirmed'
PROJECT_LINK_FIELD = 'Project'


def _fields(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (record or {}).get('fields', {}) or {}


def needs_parsing(record: Dict[str, Any]) -> bool:
    """Новая находка, которую еще не разбирали."""
    return _fields(record).get('Status') == 'New'


def is_confirmed(record: Dict[str, Any]) -> bool:
    """Человек подтвердил находку галочкой Confirmed."""
    return bool(_fields(record).get(CONFIRMED_FIELD))


def already_promoted(record: Dict[str, Any]) -> bool:
    """
    Запись уже перенесена в основную базу.

    Признак — заполненная связь с Projects: ее проставляют последним шагом
    переноса. Это и есть защита от повторного создания при падении посередине.
    """
    return bool(_fields(record).get(PROJECT_LINK_FIELD))


def load_parsed(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Разобранный ранее JSON. None, если его нет или он битый."""
    raw = _fields(record).get(PARSED_JSON_FIELD)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.error(f"Битый Parsed JSON в записи {record.get('id')}: {e}")
        return None
    return data if isinstance(data, dict) else None


def should_promote(record: Dict[str, Any]) -> bool:
    """
    Можно ли переносить запись в основную базу.

    Три условия сразу: человек подтвердил, разбор уже есть, и перенос еще
    не выполнялся.
    """
    if not is_confirmed(record):
        return False
    if already_promoted(record):
        return False
    return load_parsed(record) is not None


def promotion_blockers(record: Dict[str, Any]) -> list:
    """Почему запись не переносится — для понятного лога."""
    reasons = []
    if not is_confirmed(record):
        reasons.append('нет отметки Confirmed')
    if already_promoted(record):
        reasons.append('уже перенесена')
    if load_parsed(record) is None:
        reasons.append('нет разобранного JSON')
    return reasons
