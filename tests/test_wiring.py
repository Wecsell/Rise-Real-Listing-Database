"""
Тесты подключения модулей (wiring).

Главная проблема, которую они ловят: код написан и покрыт юнит-тестами, но не
вызывается из точки входа. Так вышло с process_generic_link — Notion, Google
Drive, Dropbox, защита от циклов и алерты по приватным ссылкам были написаны,
а listener продолжал звать старый fetch_and_parse_link, умеющий только Google
Sheets. Юнит-тесты при этом проходили: они дергали функцию напрямую.

Проверки идут по исходникам, а не через import: импорт listener тянет telethon
и переменные окружения, что делает тест зависимым от среды.
"""
import ast
import os
import re
import unittest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Модули пакета app, которые нельзя импортировать «плоско»
APP_MODULES = {
    'airtable_client', 'database', 'doc_parser', 'export_airtable',
    'gemini_parser', 'google_parser', 'healthcheck', 'history_scanner',
    'link_fetcher', 'listener', 'phone_formatter', 'priority_parser',
    'reparse_db', 'sync_job', 'whatsapp_client',
}


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestLinkProcessingIsWired(unittest.TestCase):

    def test_listener_uses_process_generic_link(self):
        src = _read(os.path.join(APP_DIR, 'listener.py'))
        self.assertIn('process_generic_link', src,
                      "listener должен вызывать process_generic_link, иначе Notion/Drive/"
                      "Dropbox и защита от циклов не работают в проде")
        self.assertNotIn('fetch_and_parse_link', src,
                         "listener не должен звать старый fetch_and_parse_link — он умеет "
                         "только Google Sheets")

    def test_history_scanner_uses_process_generic_link(self):
        src = _read(os.path.join(APP_DIR, 'history_scanner.py'))
        self.assertIn('process_generic_link', src)
        self.assertNotIn('fetch_and_parse_link', src)


class TestNoFlatIntraAppImports(unittest.TestCase):
    """
    Плоский `from gemini_parser import ...` внутри пакета создавал второй объект
    модуля в sys.modules: app.gemini_parser и gemini_parser жили параллельно со
    своими клиентами и своим кэшем модели.
    """

    def test_all_intra_app_imports_are_prefixed(self):
        offenders = []
        for fname in os.listdir(APP_DIR):
            if not fname.endswith('.py'):
                continue
            path = os.path.join(APP_DIR, fname)
            tree = ast.parse(_read(path), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    root = (node.module or '').split('.')[0]
                    if root in APP_MODULES:
                        offenders.append(f"{fname}:{node.lineno} -> from {node.module} import ...")

        self.assertEqual(offenders, [],
                         "Внутренние импорты должны идти через app.*, иначе модуль "
                         f"загрузится дважды. Нарушения: {offenders}")

    def test_app_is_a_real_package(self):
        self.assertTrue(os.path.exists(os.path.join(APP_DIR, '__init__.py')),
                        "app/__init__.py обязателен: без него app — namespace-пакет, "
                        "и двойная загрузка модулей снова становится возможной")


class TestDependenciesDeclared(unittest.TestCase):
    """
    Контейнер падал на старте: listener.py импортирует requests, которого не было
    в requirements.txt. Dockerfile ставит только этот файл.
    """

    THIRD_PARTY = {
        'requests': 'requests',
        'pypdf': 'pypdf',
        'pandas': 'pandas',
        'telegram': 'python-telegram-bot',
        'telethon': 'telethon',
        'httpx': 'httpx',
        'pydantic': 'pydantic',
        'dotenv': 'python-dotenv',
        'pyairtable': 'pyairtable',
        'asyncpg': 'asyncpg',
        'gpxpy': 'gpxpy',
        'folium': 'folium',
    }

    def test_all_imported_packages_are_in_requirements(self):
        reqs = _read(os.path.join(ROOT_DIR, 'requirements.txt')).lower()
        declared = {
            re.split(r'[=<>~\[]', line.strip())[0]
            for line in reqs.splitlines()
            if line.strip() and not line.strip().startswith('#')
        }

        missing = []
        for fname in os.listdir(APP_DIR):
            if not fname.endswith('.py'):
                continue
            tree = ast.parse(_read(os.path.join(APP_DIR, fname)))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split('.')[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or '').split('.')[0]]

                for mod in names:
                    pkg = self.THIRD_PARTY.get(mod)
                    if pkg and pkg not in declared:
                        missing.append(f"{fname}: {mod} -> нужен пакет '{pkg}'")

        self.assertEqual(missing, [], f"Не объявлены в requirements.txt: {missing}")


if __name__ == '__main__':
    unittest.main()
