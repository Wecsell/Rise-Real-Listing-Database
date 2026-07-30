"""
Защита от второго запуска одного и того же процесса.

Процессы этого проекта запускаются из разных мест: вручную из терминала, из
Antigravity, из Claude Code. Раньше защита была только у field_processor, и
это привело к тому, что одновременно работали ЧЕТЫРЕ копии field_bot. Для
Telegram это прямой конфликт: экземпляры отбирают друг у друга getUpdates, и
часть находок листеров теряется по дороге.

Замок держится на привязке к порту localhost. Это надежнее файла с PID:
операционная система освобождает порт сама, когда процесс умирает, поэтому
после аварийного завершения не остается замка, который надо чистить руками.
"""
import logging
import socket
from typing import Optional

logger = logging.getLogger("SingleInstance")

# По порту на процесс. Значения произвольные, важно лишь чтобы не пересекались.
LOCK_PORTS = {
    'field_bot': 48211,
    'field_processor': 48210,  # исторический порт, менять не нужно
    'listener': 48212,
    'sync_job': 48213,
}

_held_locks = {}


def is_running(name: str) -> bool:
    """Занят ли замок этого процесса, то есть работает ли уже копия."""
    port = LOCK_PORTS.get(name)
    if port is None:
        raise ValueError(f"Неизвестный процесс {name!r}. Известные: {sorted(LOCK_PORTS)}")

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def acquire(name: str, exit_on_conflict: bool = True) -> Optional[socket.socket]:
    """
    Занимает замок процесса.

    Возвращает сокет, который надо держать открытым всю жизнь процесса — он
    и есть замок. При конфликте по умолчанию завершает работу: молча работать
    вторым экземпляром хуже, чем не запуститься вовсе.
    """
    port = LOCK_PORTS.get(name)
    if port is None:
        raise ValueError(f"Неизвестный процесс {name!r}. Известные: {sorted(LOCK_PORTS)}")

    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", port))
    except OSError:
        lock.close()
        message = (
            f"{name} уже запущен (порт {port} занят). "
            f"Второй экземпляр запускать нельзя: копии будут конфликтовать. "
            f"Проверить: python manage.py status"
        )
        if exit_on_conflict:
            logger.error(message)
            raise SystemExit(1)
        logger.warning(message)
        return None

    _held_locks[name] = lock
    logger.info(f"{name}: замок занят (порт {port})")
    return lock


def status() -> dict:
    """Что сейчас работает: {'field_bot': True, ...}"""
    return {name: is_running(name) for name in LOCK_PORTS}
