import asyncio
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.citations import check_quotes, is_verifiable_source, OK, SPLICED, BAD
from app.field_extractor import (
    HUMAN_CONFIRM_FIELDS,
    POSITIVE_ONLY_FIELDS,
    _verdict,
    extract_field,
    gaps_from_results,
    is_negative_answer,
    question_for,
    select_relevant_pages,
)


class TestCitationCheck(unittest.TestCase):
    """
    Логика переехала из бенчмарка, где отлаживалась на живых кейсах. Здесь
    закреплены ровно те четыре дефекта измерителя, каждый из которых когда-то
    выдавался за «провал модели» (см. план, «Главный урок про измерение»).
    """

    SRC = "The lease period is 35 years starting 2023. Land area 580 M2 per survey."

    def test_verbatim_quote_is_ok(self):
        self.assertEqual(check_quotes(["The lease period is 35 years"], self.SRC), OK)

    def test_fabricated_quote_is_bad(self):
        self.assertEqual(check_quotes(["the lease runs for 99 years"], self.SRC), BAD)

    def test_composite_answer_from_two_fragments_is_allowed(self):
        """Дефект 1: схема допускала одну цитату, составные ответы браковались."""
        self.assertEqual(
            check_quotes(["The lease period is 35 years", "Land area 580 M2"], self.SRC), OK
        )

    def test_table_quote_with_scattered_words_is_spliced_not_bad(self):
        """
        Дефект 3-й природы: у таблиц дословная непрерывная цитата структурно
        невозможна - при извлечении текста двумерная структура теряется.
        """
        self.assertEqual(check_quotes(["580 lease period"], self.SRC), SPLICED)

    def test_short_but_real_cell_values_are_not_rejected(self):
        """
        Дефект 3: минимальная длина 6 символов на КАЖДУЮ цитату браковала
        настоящие значения из ячеек таблицы ('580', 'M2') как выдумку.
        Порог суммарный - две коротких настоящих цитаты вместе проходят.
        """
        self.assertEqual(check_quotes(["580", "lease period"], self.SRC), OK)

    def test_single_too_short_quote_is_still_bad(self):
        """Обратная сторона: ответ, подпёртый одним союзом, не проходит."""
        self.assertEqual(check_quotes(["is"], self.SRC), BAD)

    def test_empty_quotes_are_bad(self):
        self.assertEqual(check_quotes([], self.SRC), BAD)
        self.assertEqual(check_quotes(None, self.SRC), BAD)

    def test_scan_without_text_layer_is_unverifiable(self):
        self.assertFalse(is_verifiable_source(""))
        self.assertFalse(is_verifiable_source("   \n  "))
        self.assertTrue(is_verifiable_source(self.SRC))


class TestVerdictRules(unittest.TestCase):
    """Судьба значения решается правилами, проверяемыми без сети."""

    SRC = "The lease period is 35 years starting 2023."

    def test_valid_citation_accepts_value(self):
        v = _verdict("35 years", ["The lease period is 35 years"], self.SRC)
        self.assertTrue(v["accepted"])

    def test_fabricated_citation_rejects_value(self):
        """План, тест 14: цитаты нет в источнике -> значение отброшено."""
        v = _verdict("17 years", ["penalty clause says 17 years"], self.SRC)
        self.assertFalse(v["accepted"])
        self.assertEqual(v["citation"], BAD)

    def test_not_stated_is_a_normal_outcome_not_an_error(self):
        """Отказ - ошибка в безопасную сторону: поле уходит в вопрос агенту."""
        v = _verdict("not stated", [], self.SRC)
        self.assertFalse(v["accepted"])
        self.assertIn("not stated", v["reason"])

    def test_scan_source_is_never_silently_accepted(self):
        """
        На чистом скане проверка неприменима - значение нельзя считать
        подтверждённым (план: 3 из 5 ответов на сканах были непроверяемыми,
        включая мастер-лиз и скриншот разрешений).
        """
        v = _verdict("35 years", ["The lease period is 35 years"], "")
        self.assertFalse(v["accepted"])
        self.assertIn("unverifiable", v["reason"])


class TestPositiveOnlyFields(unittest.TestCase):
    """
    План, тесты 11 и 12 + «Правило достоверности» Э3: Handover Permits
    заполняется ТОЛЬКО положительной находкой. Отсутствие разрешения имеет
    право констатировать агент (Э4), но никогда - разбор документа.

    Найдено живым прогоном на реальных ловушках Four Palms: модель в ловушку
    не попалась и честно ответила «разрешения нет», но код принимал этот
    отрицательный ответ как значение поля.
    """

    SRC = ("Informasi Tata Ruang: VILA - MEMUNGKINKAN. This document is not a "
           "construction permit, it is a consideration for future decisions.")

    def test_itr_negative_answer_does_not_fill_permits(self):
        v = _verdict(
            "No, the document explicitly states that this is not a construction permit.",
            ["This document is not a construction permit"], self.SRC, "Handover Permits",
        )
        self.assertFalse(v["accepted"])
        self.assertIn("positive-only", v["reason"])

    def test_simbg_in_process_does_not_fill_permits(self):
        v = _verdict(
            "No, a permit has not been issued. Status 'Perbaikan Dokumen', 0 completed.",
            ["Perbaikan Dokumen"], self.SRC, "Handover Permits",
        )
        self.assertFalse(v["accepted"])

    def test_positive_finding_is_accepted(self):
        """Обратная сторона: настоящая положительная находка проходит."""
        src = "PBG number 1234 has been issued for this building on 12 May 2026."
        v = _verdict("PBG issued", ["PBG number 1234 has been issued"], src, "Handover Permits")
        self.assertTrue(v["accepted"])

    def test_negation_detection_respects_word_boundaries(self):
        """
        Тот же класс бага, что 'are' внутри 'share': подстрока 'no' сидит
        внутри 'notarial' и 'nomor', 'not' - внутри 'notice'.
        """
        self.assertFalse(is_negative_answer("PBG nomor 1234 issued by notarial deed"))
        self.assertTrue(is_negative_answer("No permit issued"))

    def test_rule_applies_only_to_positive_only_fields(self):
        """У обычного поля отрицательный ответ - нормальное значение."""
        src = "The ownership is not freehold, it is leasehold for 30 years."
        v = _verdict("not freehold, leasehold", ["The ownership is not freehold"],
                     src, "Ownership Type")
        self.assertTrue(v["accepted"])


class TestRiskyFieldsNeedHuman(unittest.TestCase):
    """План, п.6: юридические и рисковые поля модель не заполняет сама."""

    def test_legal_fields_are_marked(self):
        for field in ("Handover Permits", "Land Zoning Color", "Lease Term (years)"):
            self.assertIn(field, HUMAN_CONFIRM_FIELDS)

    def test_narrow_question_exists_for_every_risky_field(self):
        for field in HUMAN_CONFIRM_FIELDS:
            if field == "Renewal Right":
                continue  # пока не заведено, поле не в REQUIRED_PROJECT_FIELDS
            self.assertIsNotNone(question_for(field), field)

    def test_permit_question_names_the_known_traps(self):
        """
        Ловушки из живого разбора Four Palms: ITR это зонирование, а не
        разрешение; заявка в процессе - не выданное разрешение.
        """
        q = question_for("Handover Permits").lower()
        self.assertIn("not a building permit", q)
        self.assertIn("in process", q)


class TestPageSlicing(unittest.TestCase):
    """План, п.4: не отдавать 10 страниц - найти нужную и отдать 1-2."""

    DOC = (
        "--- Страница 1 ---\nGeneral introduction about the villa complex.\n\n"
        "--- Страница 2 ---\nPayment schedule and bank details.\n\n"
        "--- Страница 3 ---\nPBG permit registration and SLF status here.\n\n"
        "--- Страница 4 ---\nContacts.\n"
    )

    def test_relevant_page_is_selected_for_field(self):
        out = select_relevant_pages(self.DOC, "Handover Permits", max_pages=1)
        self.assertIn("PBG permit registration", out)
        self.assertNotIn("Payment schedule", out)

    def test_short_document_is_returned_whole(self):
        short = "--- Страница 1 ---\nEverything is here."
        self.assertEqual(select_relevant_pages(short, "Handover Permits"), short)

    def test_no_page_markers_returns_text_unchanged(self):
        plain = "just a flat text without page markers"
        self.assertEqual(select_relevant_pages(plain, "Handover Permits"), plain)

    def test_no_match_falls_back_to_document_start(self):
        """Врать про релевантность хуже, чем не угадать: берём начало."""
        doc = ("--- Страница 1 ---\nA\n\n--- Страница 2 ---\nB\n\n"
               "--- Страница 3 ---\nC\n")
        out = select_relevant_pages(doc, "Handover Permits", max_pages=1)
        self.assertIn("A", out)


class TestExtractFieldWiring(unittest.TestCase):

    SRC = "--- Страница 1 ---\nThe lease period is 35 years starting 2023."

    def _run(self, model_json):
        async def run_test():
            fake_resp = MagicMock(text=model_json)
            fake_client = MagicMock()
            fake_client.aio.models.generate_content = AsyncMock(return_value=fake_resp)
            with patch('app.field_extractor.client', fake_client):
                return await extract_field(self.SRC, "Lease Term (years)")

        return asyncio.run(run_test())

    def test_good_answer_is_accepted_and_flagged_for_human(self):
        res = self._run('{"answer": "35", "quotes": ["The lease period is 35 years"], "confidence": 0.9}')
        self.assertTrue(res["accepted"])
        self.assertEqual(res["value"], "35")
        self.assertTrue(res["needs_human"], "срок аренды - рисковое поле, нужен Confirmed")

    def test_hallucinated_answer_yields_no_value(self):
        res = self._run('{"answer": "17", "quotes": ["only valid for 17 years"], "confidence": 0.95}')
        self.assertFalse(res["accepted"])
        self.assertIsNone(res["value"])

    def test_template_echo_is_skipped_and_real_answer_taken(self):
        """
        Часть моделей повторяет шаблон из промпта и только потом даёт ответ -
        два JSON-объекта подряд, json.loads на такой строке падает. Бенчмарк
        берёт ПЕРВЫЙ объект, но первым как раз идёт шаблон с плейсхолдером,
        то есть измеряется шаблон вместо ответа модели.
        """
        double = ('{"answer": "<short answer>", "quotes": [], "confidence": 0.0}'
                  '{"answer": "35", "quotes": ["The lease period is 35 years"], "confidence": 0.9}')
        res = self._run(double)
        self.assertEqual(res["value"], "35")
        self.assertTrue(res["accepted"])

    def test_fenced_json_is_parsed(self):
        res = self._run('```json\n{"answer": "35", "quotes": ["The lease period is 35 years"]}\n```')
        self.assertEqual(res["value"], "35")

    def test_garbage_response_is_reported_not_raised(self):
        res = self._run("I cannot answer that question.")
        self.assertFalse(res["accepted"])
        self.assertIsNone(res["value"])

    def test_api_error_is_reported_not_raised(self):
        async def run_test():
            fake_client = MagicMock()
            fake_client.aio.models.generate_content = AsyncMock(side_effect=Exception("quota"))
            with patch('app.field_extractor.client', fake_client):
                return await extract_field(self.SRC, "Lease Term (years)")

        res = asyncio.run(run_test())
        self.assertFalse(res["accepted"])
        self.assertIn("quota", res["reason"])

    def test_unknown_field_without_question_is_refused(self):
        async def run_test():
            return await extract_field(self.SRC, "Some Field We Never Defined")

        res = asyncio.run(run_test())
        self.assertFalse(res["accepted"])
        self.assertIn("no narrow question", res["reason"])


class TestGapsFromResults(unittest.TestCase):

    def test_rejected_values_become_visible_gaps(self):
        results = [
            {"field": "Lease Term (years)", "accepted": False, "reason": "citation not found in source"},
            {"field": "Developer", "accepted": True, "value": "PT Seacrest"},
        ]
        gaps = gaps_from_results(results)
        self.assertEqual(len(gaps), 1)
        self.assertIn("Lease Term (years)", gaps[0])


if __name__ == '__main__':
    unittest.main()
