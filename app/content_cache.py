"""
Локальный кэш результатов разбора через Gemini.

Зачем это нужно: агент может переслать один и тот же PDF или один и тот же
текст дважды в чат (например, повторно скинуть Dev Kit после вопроса), а
listener сканирует историю чата заново при каждом перезапуске. Без кэша
такой контент разбирается через Gemini повторно — тот же текст, тот же
ответ, но за деньги (или за квоту бесплатного уровня, которая исчерпывается
быстрее денег).

Хранится в SQLite-файле рядом с проектом, а не в Postgres: Postgres в этом
окружении не поднят (Docker не установлен, `asyncpg` не стоит — `init_db()`
в app/database.py молча делает ничего), а кэш должен работать независимо
от того, поднята инфраструктура или нет.

Ключ — хэш контента, а не URL или message_id: одно и то же содержимое,
пришедшее по разным ссылкам или в разных сообщениях, должно найти один и
тот же кэш.
"""
import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("ContentCache")

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'gemini_cache.db',
)

# Сколько хранить результат. Достаточно долго, чтобы поймать пересылки
# и повторные сканы истории, но не бесконечно — если промпт или каноны
# базы поменяются, старые записи не должны действовать вечно.
CACHE_TTL_DAYS = int(os.environ.get('GEMINI_CACHE_TTL_DAYS', '30'))

SEPARATOR = '␞'  # редкий управляющий символ — безопасный разделитель


def _db_path() -> str:
    return os.environ.get('GEMINI_CACHE_DB_PATH', _DB_PATH)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_cache (
            hash TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    return conn


def hash_text(text: str, context: str = '') -> str:
    """
    Хэш текста для разбора через parse_message.

    context — обычно название чата: один и тот же текст в разных чатах может
    получить разное разрешение неоднозначностей (какой застройщик имелся в
    виду), поэтому попадает в ключ.
    """
    payload = f"{context or ''}{SEPARATOR}{text or ''}"
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def hash_file(path: str, chunk_size: int = 1 << 20) -> str:
    """Хэш файла на диске, без загрузки его целиком в память."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def get(key: str) -> Optional[Dict[str, Any]]:
    """Разобранный результат из кэша, если он есть и не просрочен."""
    try:
        conn = _connect()
        try:
            cutoff = time.time() - CACHE_TTL_DAYS * 86400
            row = conn.execute(
                "SELECT result FROM gemini_cache WHERE hash = ? AND created_at >= ?",
                (key, cutoff),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Кэш недоступен на чтение, работаем без него: {e}")
        return None

    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def put(key: str, kind: str, result: Dict[str, Any]) -> None:
    """
    Сохраняет результат разбора. Ошибки записи не должны ронять пайплайн —
    кэш это оптимизация, а не источник истины.
    """
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO gemini_cache (hash, kind, result, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, kind, json.dumps(result, ensure_ascii=False), time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Не удалось записать в кэш: {e}")


def is_cacheable(result: Optional[Dict[str, Any]]) -> bool:
    """
    Кэшировать стоит только успешные разборы. Ошибка вызова (нет ключа API,
    сбой сети, превышен лимит) не должна залипать в кэше — иначе временный
    сбой станет постоянным для этого контента.
    """
    if not isinstance(result, dict):
        return False
    return 'error' not in result
