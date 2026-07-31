"""
Защита от второго запуска.

Процессы поднимаются из разных мест: вручную, из Antigravity, из Claude Code.
Защита была только у field_processor, и однажды одновременно работали ЧЕТЫРЕ
копии field_bot. Для Telegram это прямой конфликт: экземпляры отбирают друг у
друга getUpdates, и часть находок листеров теряется.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.single_instance import LOCK_PORTS, acquire, is_running, status


@pytest.fixture(autouse=True)
def isolated_ports(monkeypatch):
    """
    Подменяет порты на тестовые.

    Без этого тесты дерутся за замки с реально запущенными ботами: если
    field_bot работает, тест не может занять его порт и падает — при том что
    проверяемый код исправен. Тест не должен зависеть от того, поднята ли
    сейчас боевая система.
    """
    import app.single_instance as si
    monkeypatch.setattr(si, 'LOCK_PORTS', {
        'field_bot': 48311,
        'field_processor': 48310,
        'listener': 48312,
        'sync_job': 48313,
    })
    monkeypatch.setattr(si, '_held_locks', {})


@pytest.fixture
def released():
    """Гарантирует, что после теста замки свободны."""
    held = []
    yield held
    for lock in held:
        try:
            lock.close()
        except Exception:
            pass


class TestLockLifecycle:

    def test_free_before_acquiring(self):
        assert is_running('field_bot') is False

    def test_acquiring_marks_it_running(self, released):
        lock = acquire('field_bot')
        released.append(lock)
        assert is_running('field_bot') is True

    def test_releasing_frees_it(self):
        lock = acquire('field_bot')
        lock.close()
        assert is_running('field_bot') is False

    def test_second_acquire_is_refused(self, released):
        """Главное: второй экземпляр не должен подняться."""
        first = acquire('field_bot')
        released.append(first)

        second = acquire('field_bot', exit_on_conflict=False)
        assert second is None, "второй экземпляр получил замок — копии будут конфликтовать"

    def test_second_acquire_exits_by_default(self, released):
        """
        Молча работать вторым экземпляром хуже, чем не запуститься:
        конфликт за getUpdates теряет находки, и это незаметно.
        """
        first = acquire('field_bot')
        released.append(first)

        with pytest.raises(SystemExit) as exc:
            acquire('field_bot')
        assert exc.value.code == 1


class TestIndependence:
    """Замок одного процесса не должен мешать другому."""

    def test_processes_do_not_block_each_other(self, released):
        bot = acquire('field_bot')
        released.append(bot)
        processor = acquire('field_processor', exit_on_conflict=False)
        released.append(processor)

        assert processor is not None
        assert is_running('field_bot') is True
        assert is_running('field_processor') is True

    def test_every_process_has_its_own_port(self):
        ports = list(LOCK_PORTS.values())
        assert len(ports) == len(set(ports)), f"порты пересекаются: {LOCK_PORTS}"

    def test_entry_points_are_covered(self):
        for name in ('field_bot', 'field_processor', 'listener'):
            assert name in LOCK_PORTS


class TestErrors:

    def test_unknown_process_name_is_rejected(self):
        with pytest.raises(ValueError):
            is_running('какой-то-другой-бот')
        with pytest.raises(ValueError):
            acquire('какой-то-другой-бот')


class TestStatusReport:

    def test_status_covers_all_known_processes(self):
        report = status()
        assert set(report) == set(LOCK_PORTS)
        assert all(isinstance(v, bool) for v in report.values())

    def test_status_reflects_a_held_lock(self, released):
        lock = acquire('listener')
        released.append(lock)
        assert status()['listener'] is True
