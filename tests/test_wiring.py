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


class TestFieldProcessorPipeline(unittest.TestCase):
    """
    Разбор и перенос должны оставаться разными фазами. Если запись в основную
    базу снова окажется в фазе разбора, находка с баннера начнет попадать в
    Projects без подтверждения человека.
    """

    def setUp(self):
        self.src = _read(os.path.join(ROOT_DIR, 'field_processor.py'))
        self.parse_phase = self.src.split('async def promote_confirmed_findings')[0]

    def test_both_phases_are_called(self):
        entry = self.src.split('async def promote_confirmed_findings')[0]
        self.assertIn('await parse_new_findings()', entry)
        self.assertIn('await promote_confirmed_findings()', entry)

    def test_parse_phase_never_writes_to_main_tables(self):
        for fn in ('upsert_project', 'upsert_unit', 'upsert_developer'):
            self.assertNotIn(
                fn, self.parse_phase,
                f"{fn} не должен вызываться на этапе разбора — перенос идет "
                f"только по галочке Confirmed"
            )

    def test_promotion_is_gated_and_guarded(self):
        self.assertIn('should_promote', self.src,
                      "перенос обязан проверять Confirmed и признак уже выполненного переноса")
        self.assertIn("{Confirmed} = 1", self.src)

    def test_cache_is_refreshed_before_duplicate_lookup(self):
        promote = self.src.split('async def promote_confirmed_findings')[1]
        self.assertIn('init_cache_async(force=True)', promote,
                      "без свежего кэша поиск дубля не увидит записи других процессов")

    def test_duplicate_detection_is_wired(self):
        self.assertIn('find_duplicates', self.parse_phase)
        self.assertIn('notify_lister', self.parse_phase)


class TestFieldBotAttribution(unittest.TestCase):

    def test_bot_records_who_submitted(self):
        src = _read(os.path.join(ROOT_DIR, 'field_bot.py'))
        self.assertIn('Submitted By', src)
        self.assertIn('Telegram Chat ID', src)
        self.assertIn('is_allowed', src,
                      "доступ должен идти через белый список, а не сравнение с одним id")


class TestNoDeadExports(unittest.TestCase):
    """
    Каждая публичная функция должна кем-то вызываться вне своего модуля и вне
    тестов. Иначе получается то же самое, что уже случилось в этом проекте:
    код написан, покрыт зелеными тестами и никем не используется.
    """

    AUTHORED_MODULES = ['staging.py', 'gaps.py', 'dedup.py', 'access.py', 'schema_check.py']

    def test_every_public_function_is_called_somewhere(self):
        """
        Мертвой считается функция, на которую в рабочем коде нет ни одной
        ссылки кроме собственного def. Вызов из соседней функции того же
        модуля — нормальная композиция, а не мертвый код; поэтому смотрим на
        любые упоминания, а не только на межмодульные.

        Тесты в подсчет не идут намеренно: покрытая тестами функция, которую
        никто не вызывает, — это ровно тот случай, который здесь ловится.
        """
        sources = []
        for root, _dirs, files in os.walk(ROOT_DIR):
            if any(skip in root for skip in ('.git', '__pycache__', 'tests', '.agent')):
                continue
            for f in files:
                if f.endswith('.py'):
                    sources.append(_read(os.path.join(root, f)))

        orphans = []
        for module in self.AUTHORED_MODULES:
            path = os.path.join(APP_DIR, module)
            tree = ast.parse(_read(path), filename=path)
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith('_') or node.name == 'main':
                    continue

                references = sum(src.count(node.name) for src in sources)
                # Одно упоминание — это сама строка `def name(...)`
                if references <= 1:
                    orphans.append(f"{module}:{node.name}")

        self.assertEqual(orphans, [],
                         f"Функции без единого вызова в рабочем коде: {orphans}")


if __name__ == '__main__':
    unittest.main()
