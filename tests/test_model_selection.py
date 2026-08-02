"""
Выбор модели T1 (разбор сообщений чата) по implementation_plan.md.

План ("Выбор моделей по уровням") фиксирует T1 = gemini-3.5-flash-lite,
T2 (документы) = gemini-3.5-flash. T2 в field_extractor.DOC_MODEL стоял верно
с самого начала, а T1 остался на gemini-2.5-flash - решение плана до парсера
чата не доехало. Эти тесты держат обе точки в соответствии с планом.

Отдельно проверяется ловушка подстроки: 'gemini-3.5-flash' является подстрокой
'gemini-3.5-flash-lite', и отбор через `if preferred in name` молча выбирает
lite там, где просили полную модель (или наоборот) - в зависимости от порядка
выдачи API. Тот же класс бага, что 'are' внутри 'share' в предфильтре
listener.py и 'no' внутри 'nomor' в детекторе отрицания field_extractor.
"""
from types import SimpleNamespace
from unittest.mock import patch

import app.gemini_parser as gemini_parser
from app.field_extractor import DOC_MODEL


def _fake_models(names):
    """Клиент, отдающий заданный список моделей из client.models.list()."""
    return SimpleNamespace(
        models=SimpleNamespace(list=lambda: [SimpleNamespace(name=n) for n in names])
    )


class TestModelMatchesPlan:
    def test_t2_document_model_is_flash_35(self):
        """T2 по плану - полная gemini-3.5-flash (единственная со 100% и чистыми цитатами)."""
        assert DOC_MODEL == "gemini-3.5-flash"

    def test_t1_default_model_is_flash_lite_35(self):
        """T1 по плану - gemini-3.5-flash-lite: втрое быстрее при той же цене за токен."""
        assert gemini_parser.DEFAULT_MODEL == "gemini-3.5-flash-lite"

    def test_t1_preferred_list_starts_with_plan_choice(self):
        assert gemini_parser.PREFERRED_MODELS[0] == "gemini-3.5-flash-lite"


class TestModelResolutionAgainstLiveList:
    """
    Список имён взят из реального ответа client.models.list() на боевом ключе
    (проверено 02.08.2026), включая порядок, в котором 'gemini-3.5-flash'
    идёт РАНЬШЕ 'gemini-3.5-flash-lite'.
    """

    LIVE_NAMES = [
        "models/gemini-2.0-flash",
        "models/gemini-2.5-flash",
        "models/gemini-2.5-flash-lite",
        "models/gemini-3-flash-preview",
        "models/gemini-3.1-flash-lite",
        "models/gemini-3.5-flash",
        "models/gemini-3.5-flash-lite",
        "models/gemini-3.6-flash",
        "models/gemini-flash-latest",
    ]

    def setup_method(self):
        gemini_parser._cached_model_name = None

    def teardown_method(self):
        gemini_parser._cached_model_name = None

    def test_resolves_to_flash_lite_not_full_flash(self):
        """
        Регрессия на ловушку подстроки: 'gemini-3.5-flash' стоит в живой выдаче
        РАНЬШЕ 'gemini-3.5-flash-lite', и наивное `preferred in name` вернуло бы
        полную модель вместо lite, которую просит план.
        """
        with patch.object(gemini_parser, 'client', _fake_models(self.LIVE_NAMES)):
            assert gemini_parser.resolve_model_name() == "models/gemini-3.5-flash-lite"

    def test_falls_back_to_next_preferred_when_plan_model_absent(self):
        """Модели из плана нет в выдаче - берём следующую по списку, а не первую попавшуюся."""
        without_35_lite = [n for n in self.LIVE_NAMES if n != "models/gemini-3.5-flash-lite"]
        with patch.object(gemini_parser, 'client', _fake_models(without_35_lite)):
            assert gemini_parser.resolve_model_name() == "models/gemini-3.5-flash"

    def test_empty_model_list_falls_back_to_default(self):
        with patch.object(gemini_parser, 'client', _fake_models([])):
            assert gemini_parser.resolve_model_name() == gemini_parser.DEFAULT_MODEL

    def test_no_client_falls_back_to_default_without_crashing(self):
        with patch.object(gemini_parser, 'client', None):
            assert gemini_parser.resolve_model_name() == gemini_parser.DEFAULT_MODEL
