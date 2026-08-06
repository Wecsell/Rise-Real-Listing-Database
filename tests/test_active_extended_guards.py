"""
Продолжение test_active_protects_record.py: три дыры, найденные ревью на Опусе
06.08.2026 для того же требования владельца («Active на проекте — юниты и
проект не перезаписываются»).

1. SKIPPED_ACTIVE обязан быть ИСТИННЫМ и отличимым от None: до фикса
   upsert_unit возвращал обычный None и при защите, и при настоящем сбое
   записи — field_processor.py и app/sync_job.py читают результат только как
   `if not unit_id: <считать сбоем>`, поэтому Active-проект выглядел как
   упавшая запись: Field Staging уходила в вечный повтор каждые 30 секунд,
   очередь sync_job — в failed после исчерпания ретраев.
2. mark_project_units_sold() должен уважать Active так же, как upsert_unit —
   функция сейчас нигде не вызывается (мёртвый код), но заряжена тем же багом.
3. doc_pipeline.save_findings_to_gaps() пишет Gaps/Renders/Img в обход
   upsert_project — рабочий автоматический путь, который защиту не проверял.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.airtable_client as ac


class TestSkippedActiveSentinel:

    def test_is_truthy(self):
        assert bool(ac.SKIPPED_ACTIVE) is True
        assert not (not ac.SKIPPED_ACTIVE)

    def test_field_processor_pattern_does_not_raise(self):
        """Точная форма проверки из field_processor.py:210 и app/sync_job.py:277."""
        unit_id = ac.SKIPPED_ACTIVE
        triggered = False
        if not unit_id:
            triggered = True
        assert triggered is False, "SKIPPED_ACTIVE не должен читаться как сбой записи"

    def test_distinguishable_from_none(self):
        assert ac.SKIPPED_ACTIVE is not None
        assert ac.SKIPPED_ACTIVE != None  # noqa: E711 — сознательно проверяем __eq__, не только is

    def test_real_failure_is_still_falsy(self):
        """Настоящий сбой (upsert_unit возвращает None) обязан продолжать читаться как сбой."""
        unit_id = None
        assert not unit_id


class TestIsProjectActive:

    def test_true_when_flag_set(self, monkeypatch):
        monkeypatch.setattr(ac, 'CACHE_PROJECTS',
                             [{'id': 'recX', 'fields': {'Active': True}}])
        assert ac.is_project_active('recX') is True

    def test_false_when_flag_unset(self, monkeypatch):
        monkeypatch.setattr(ac, 'CACHE_PROJECTS',
                             [{'id': 'recX', 'fields': {'Active': False}}])
        assert ac.is_project_active('recX') is False

    def test_false_when_field_absent(self, monkeypatch):
        """Airtable для непроставленного чекбокса поле не возвращает вовсе."""
        monkeypatch.setattr(ac, 'CACHE_PROJECTS',
                             [{'id': 'recX', 'fields': {}}])
        assert ac.is_project_active('recX') is False

    def test_false_when_id_not_in_cache(self, monkeypatch):
        monkeypatch.setattr(ac, 'CACHE_PROJECTS', [])
        assert ac.is_project_active('recUnknown') is False


class TestMarkProjectUnitsSoldRespectsActive:

    @pytest.mark.asyncio
    async def test_active_project_units_not_touched(self, monkeypatch):
        monkeypatch.setattr(ac, 'cache_is_stale', lambda: False)
        monkeypatch.setattr(ac, 'CACHE_PROJECTS',
                             [{'id': 'recActive', 'fields': {'Active': True}}])
        table_primary = MagicMock()
        table_secondary = MagicMock()
        monkeypatch.setattr(ac, 'get_table',
                             lambda name: table_primary if name == 'Units' else table_secondary)

        await ac.mark_project_units_sold('recActive')

        table_primary.update.assert_not_called()
        table_secondary.create.assert_not_called()


class TestDocPipelineRespectsActive:

    @pytest.mark.asyncio
    async def test_active_project_gaps_not_updated(self, monkeypatch):
        from app import doc_pipeline

        table = MagicMock()
        table.get.return_value = {'fields': {'Active': True, 'Gaps': 'старое'}}
        monkeypatch.setattr(ac, 'get_table', lambda _name: table)
        robust_spy = MagicMock()
        monkeypatch.setattr(ac, 'robust_airtable_op', robust_spy)

        changed = await doc_pipeline.save_findings_to_gaps(
            'recActive', {'proposed': {'District': 'Nuanu'}},
        )

        assert changed is False
        robust_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_inactive_project_still_updates(self, monkeypatch):
        """Регресс: без Active поведение должно остаться прежним."""
        from app import doc_pipeline

        table = MagicMock()
        table.get.return_value = {'fields': {'Active': False, 'Gaps': ''}}
        monkeypatch.setattr(ac, 'get_table', lambda _name: table)
        monkeypatch.setattr(ac, 'robust_airtable_op',
                             lambda *a, **kw: {'id': 'recX', 'fields': {}})

        changed = await doc_pipeline.save_findings_to_gaps(
            'recX', {'proposed': {'District': 'Nuanu'}},
        )

        assert changed is True
