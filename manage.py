#!/usr/bin/env python
"""
Единая точка запуска и остановки процессов проекта.

Процессы поднимаются из разных мест: вручную из терминала, из Antigravity, из
Claude Code. Сами по себе это обычные python-скрипты, и запустить их можно
откуда угодно — проблема не в запуске, а в столкновении. Однажды одновременно
работали четыре копии field_bot: экземпляры отбирают друг у друга getUpdates
Telegram, и часть находок листеров теряется.

Здесь один способ на всех: сначала смотрим статус, потом запускаем. Замок
не даст поднять второй экземпляр, чей бы запуск это ни был.

    python manage.py status
    python manage.py start field_bot
    python manage.py start all
    python manage.py stop field_bot
    python manage.py stop all
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.single_instance import LOCK_PORTS, is_running

# Чем запускается каждый процесс
COMMANDS = {
    'field_bot': [sys.executable, 'field_bot.py'],
    'field_processor': [sys.executable, 'field_processor.py'],
    'listener': [sys.executable, '-m', 'app.listener'],
}

DESCRIPTIONS = {
    'field_bot': 'приём фото, аудио, геолокаций и GPX от листеров',
    'field_processor': 'разбор находок и перенос подтверждённых в основную базу',
    'listener': 'чтение чатов застройщиков в Telegram',
}

ROOT = os.path.dirname(os.path.abspath(__file__))


def cmd_status(_args) -> int:
    print(f"{'процесс':18} {'состояние':12} что делает")
    print('-' * 78)
    for name in COMMANDS:
        state = 'РАБОТАЕТ' if is_running(name) else 'остановлен'
        print(f"{name:18} {state:12} {DESCRIPTIONS.get(name, '')}")

    unknown = set(LOCK_PORTS) - set(COMMANDS)
    for name in sorted(unknown):
        if is_running(name):
            print(f"{name:18} {'РАБОТАЕТ':12} (запускается отдельно)")
    return 0


def _start_one(name: str) -> bool:
    if is_running(name):
        print(f"  {name}: уже работает, пропускаю")
        return True

    print(f"  {name}: запускаю...")
    try:
        subprocess.Popen(COMMANDS[name], cwd=ROOT)
    except Exception as e:
        print(f"  {name}: не удалось запустить — {e}")
        return False
    return True


def cmd_start(args) -> int:
    targets = list(COMMANDS) if args.name == 'all' else [args.name]
    ok = all(_start_one(n) for n in targets)
    print("\nПроверить: python manage.py status")
    return 0 if ok else 1


def _stop_one(name: str) -> bool:
    """Останавливает процесс по строке запуска — замок освободится сам."""
    if not is_running(name):
        print(f"  {name}: и так остановлен")
        return True

    marker = COMMANDS[name][-1]
    if sys.platform == 'win32':
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            f"Where-Object {{ $_.CommandLine -match '{marker}' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        cmd = ['powershell', '-NoProfile', '-Command', script]
    else:
        cmd = ['pkill', '-f', marker]

    subprocess.run(cmd, capture_output=True)
    print(f"  {name}: остановлен")
    return True


def cmd_stop(args) -> int:
    targets = list(COMMANDS) if args.name == 'all' else [args.name]
    for n in targets:
        _stop_one(n)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('status', help='что сейчас работает')

    choices = list(COMMANDS) + ['all']
    p_start = sub.add_parser('start', help='запустить процесс')
    p_start.add_argument('name', choices=choices)

    p_stop = sub.add_parser('stop', help='остановить процесс')
    p_stop.add_argument('name', choices=choices)

    args = parser.parse_args()
    return {'status': cmd_status, 'start': cmd_start, 'stop': cmd_stop}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
