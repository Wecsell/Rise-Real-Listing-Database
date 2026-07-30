"""
Доступ к полевому боту и подпись находки.

Раньше в .env лежал ровно один ALLOWED_TELEGRAM_USER_ID, и проверка была
строгим сравнением строк. Работать с ботом мог только один человек, а в
Field Staging не было ни следа того, кто прислал находку. Листеров несколько,
и находки нужно различать по автору — как для контроля покрытия, так и чтобы
отправить уведомление о дубле именно тому, кто едет по маршруту.
"""
import logging
from typing import Any, Optional, Set

logger = logging.getLogger("Access")

# Разделители, которые человек реально напишет в .env
_SEPARATORS = (',', ';', '\n', ' ')


def parse_allowed_users(raw: Optional[str]) -> Set[str]:
    """
    Разбирает список разрешенных Telegram ID из переменной окружения.

    Формат остается совместимым с прежним: одно число тоже валидно.
    Допускаются запятые, точки с запятой, пробелы и переносы строк.
    """
    if not raw:
        return set()

    normalized = str(raw)
    for sep in _SEPARATORS[1:]:
        normalized = normalized.replace(sep, ',')

    users = set()
    for part in normalized.split(','):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip('-').isdigit():
            logger.warning(f"ALLOWED_TELEGRAM_USER_ID: пропускаю нечисловое значение {part!r}")
            continue
        users.add(part)
    return users


def is_allowed(user_id: Any, raw: Optional[str]) -> bool:
    """Разрешен ли пользователь. Пустой список означает «никому», а не «всем»."""
    allowed = parse_allowed_users(raw)
    if not allowed:
        return False
    return str(user_id) in allowed


def describe_user(user) -> str:
    """
    Подпись листера для поля Submitted By.

    Приоритет: @username (по нему человека проще найти), затем имя и фамилия,
    затем числовой id как последний вариант.
    """
    if user is None:
        return 'unknown'

    username = getattr(user, 'username', None)
    if username:
        return f"@{username}"

    parts = [getattr(user, 'first_name', None), getattr(user, 'last_name', None)]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name

    user_id = getattr(user, 'id', None)
    return str(user_id) if user_id is not None else 'unknown'
