"""
Проверки выключателя обращений к LLM.

Главное здесь — не то, что клиент равен None, а то, что выключенный разбор
НЕ выдаёт себя за выполненный. Именно на этом теряются данные: карточка с
is_relevant=False выглядит проверенной и пустой, хотя её никто не смотрел.
"""
import asyncio
import importlib
import os

import pytest

from app import llm_gate


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm_gate._warned = False


def test_backend_defaults_to_off():
    """Молчаливое обращение к API не должно быть возможно по умолчанию:
    отсутствие LLM_BACKEND означает off, а не 'включи, если есть ключ'."""
    assert llm_gate.backend() == llm_gate.BACKEND_OFF
    assert llm_gate.llm_enabled() is False


def test_key_alone_does_not_enable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert llm_gate.llm_enabled() is False


def test_gemini_needs_both_backend_and_key(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "gemini")
    assert llm_gate.llm_enabled() is False, "без ключа режим gemini не включается"

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert llm_gate.llm_enabled() is True


def test_unknown_backend_falls_back_to_off(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "chatgpt-maybe")
    assert llm_gate.backend() == llm_gate.BACKEND_OFF


def test_manual_required_is_not_a_verdict():
    result = llm_gate.manual_required("parse of some listing")
    assert result["status"] == llm_gate.MANUAL_REQUIRED
    # Ни True, ни False: утверждения о содержимом источника не делается.
    assert "is_relevant" not in result


def test_parse_message_does_not_claim_irrelevant(monkeypatch):
    """Регресс: раньше выключенный парсер возвращал is_relevant=False, и
    сообщение застройщика тихо отбрасывалось как нерелевантное."""
    monkeypatch.setenv("LLM_BACKEND", "off")
    import app.gemini_parser as gp
    importlib.reload(gp)

    result = asyncio.run(gp.parse_message("Продаём виллы в Чангу, 2 спальни, от 300k"))

    assert result["is_relevant"] is None, "выключенный разбор не выносит вердикт"
    assert result["status"] == llm_gate.MANUAL_REQUIRED
    # Потребители фильтруют по truthiness, поэтому запись всё равно не создаётся.
    assert not result["is_relevant"]
