import asyncio
import os
import sys
import unittest
from unittest.mock import patch, AsyncMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.doc_pipeline import (
    GAPS_SECTION_START,
    collect_project_links,
    combine_findings,
    empty_required_fields,
    fill_fields_from_drive_files,
    fill_project_fields_from_text,
    format_findings_block,
    merge_into_gaps,
    save_findings_to_gaps,
)


def _f(name, mime="application/pdf", path="", fid=None):
    return {"id": fid or name, "name": name, "mimeType": mime, "path": path}


def _accepted(field, value="X"):
    return {"field": field, "value": value, "accepted": True, "citation": "ok",
            "quotes": ["q"], "needs_human": False, "reason": None}


def _rejected(field, reason="citation not found in source"):
    return {"field": field, "value": None, "accepted": False, "citation": "bad",
            "quotes": [], "needs_human": False, "reason": reason}


class TestEmptyRequiredFields(unittest.TestCase):

    def test_filled_fields_are_excluded(self):
        empty = empty_required_fields({"Project Name": "Four Palms", "District": "Tabanan"})
        self.assertNotIn("Project Name", empty)
        self.assertIn("Handover Permits", empty)

    def test_none_and_junk_count_as_empty(self):
        """is_filled: модель иногда пишет строку 'N/A' вместо пустоты."""
        empty = empty_required_fields({"District": "N/A", "Property Type": "  "})
        self.assertIn("District", empty)
        self.assertIn("Property Type", empty)


class TestPipelineWiring(unittest.TestCase):

    def _run(self, files, project_fields, extract_side_effect, download_ok=True, text="Some text"):
        async def run_test():
            with patch('app.drive_folder.download_drive_file', return_value=download_ok), \
                 patch('app.doc_pipeline._extract_text', new=AsyncMock(return_value=text)), \
                 patch('app.doc_pipeline.extract_field',
                       new=AsyncMock(side_effect=extract_side_effect)) as mock_extract:
                res = await fill_fields_from_drive_files(project_fields, files)
                res["_calls"] = [c.args[1] for c in mock_extract.await_args_list]
                return res

        return asyncio.run(run_test())

    def test_full_card_opens_nothing(self):
        """Нет пустых обязательных полей - ни одного дорогого открытия."""
        async def run_test():
            full = {k: "filled" for k in __import__(
                'app.gaps', fromlist=['x']).REQUIRED_PROJECT_FIELDS}
            return await fill_fields_from_drive_files(full, [_f("PBG permit.pdf")])

        res = asyncio.run(run_test())
        self.assertEqual(res["opened"], 0)
        self.assertEqual(res["proposals"], [])

    def test_accepted_value_becomes_a_proposal(self):
        res = self._run(
            [_f("PBG permit.pdf")],
            {"Handover Permits": None},
            lambda text, field: _accepted(field, "PBG issued"),
        )
        self.assertEqual(len(res["proposals"]), 1)
        self.assertEqual(res["proposals"][0]["source_file"], "PBG permit.pdf")

    def test_rejected_value_becomes_a_gap_not_a_proposal(self):
        res = self._run(
            [_f("PBG permit.pdf")],
            {"Handover Permits": None},
            lambda text, field: _rejected(field),
        )
        self.assertEqual(res["proposals"], [])
        self.assertTrue(any("Handover Permits" in g for g in res["gaps"]))

    def test_field_closed_by_first_doc_is_not_asked_again(self):
        """
        Второй документ того же типа не должен тратить вызовы модели на уже
        закрытое поле - это и есть экономия, ради которой строился роутер.
        """
        res = self._run(
            [_f("ITR one.pdf"), _f("PKKPR two.pdf")],
            {"Land Zoning Color": None},
            lambda text, field: _accepted(field, "Residential"),
        )
        self.assertEqual(res["_calls"].count("Land Zoning Color"), 1)

    def test_scan_without_text_layer_is_reported_not_guessed(self):
        """
        План: на чистом скане сверка цитаты невозможна, нужен другой
        предохранитель (прогон двумя моделями), который не реализован.
        Молча пропустить такой документ нельзя.
        """
        res = self._run(
            [_f("SLF reg.jpeg", mime="image/jpeg")],
            {"Handover Permits": None},
            lambda text, field: _accepted(field),
        )
        self.assertEqual(res["proposals"], [])
        self.assertTrue(any("scan without text layer" in g for g in res["gaps"]))
        self.assertEqual(res["_calls"], [], "модель не должна вызываться на скане")

    def test_failed_download_is_a_gap(self):
        res = self._run(
            [_f("PBG permit.pdf")],
            {"Handover Permits": None},
            lambda text, field: _accepted(field),
            download_ok=False,
        )
        self.assertTrue(any("download failed" in g for g in res["gaps"]))
        self.assertEqual(res["opened"], 0)

    def test_empty_text_layer_is_a_gap(self):
        res = self._run(
            [_f("PBG permit.pdf")],
            {"Handover Permits": None},
            lambda text, field: _accepted(field),
            text="   ",
        )
        self.assertTrue(any("no text layer" in g for g in res["gaps"]))

    def test_temp_file_is_removed_after_extraction(self):
        """Правило проекта: скачанные временные файлы не задерживаются на диске."""
        seen = {}

        def fake_download(file_id, dest_path):
            seen["path"] = dest_path
            with open(dest_path, "wb") as fh:
                fh.write(b"%PDF-1.4")
            return True

        async def run_test():
            with patch('app.drive_folder.download_drive_file', side_effect=fake_download), \
                 patch('app.doc_pipeline._extract_text', new=AsyncMock(return_value="text")), \
                 patch('app.doc_pipeline.extract_field',
                       new=AsyncMock(side_effect=lambda t, f: _accepted(f))):
                return await fill_fields_from_drive_files(
                    {"Handover Permits": None}, [_f("PBG permit.pdf")]
                )

        asyncio.run(run_test())
        self.assertIn("path", seen)
        self.assertFalse(os.path.exists(seen["path"]), "временный файл должен быть удалён")

    def test_passport_never_reaches_the_model(self):
        """Skip-лист роутера должен действовать и в связке, не только в юнит-тесте."""
        res = self._run(
            [_f("KITAP Passport Pronin.pdf")],
            {"Handover Permits": None},
            lambda text, field: _accepted(field),
        )
        self.assertEqual(res["_calls"], [])
        self.assertEqual(res["opened"], 0)

    def test_exclude_fields_are_not_searched_in_drive_documents(self):
        """
        Владелец, 02.08.2026: «шахматка сканируется первой... в других
        материалах ищем то, чего нет, и не смотрим туда, где не найдём то,
        что нужно». District уже закрыт шахматкой (exclude_fields) - документ,
        закрывающий ТОЛЬКО District, не должен открываться вовсе.
        """
        async def run_test():
            with patch('app.drive_folder.download_drive_file', return_value=True), \
                 patch('app.doc_pipeline._extract_text', new=AsyncMock(return_value="text")), \
                 patch('app.doc_pipeline.extract_field',
                       new=AsyncMock(side_effect=lambda t, f: _accepted(f))) as mock_extract:
                res = await fill_fields_from_drive_files(
                    _only_empty("District", "Handover Permits"),
                    [_f("location map.pdf")],  # doc_router: 'location' -> {District, Location Link}
                    exclude_fields={"District"},
                )
                res["_calls"] = [c.args[1] for c in mock_extract.await_args_list]
                return res

        res = asyncio.run(run_test())
        self.assertEqual(res["_calls"], [], "файл закрывает только District - открывать незачем")
        self.assertEqual(res["opened"], 0)


def _only_empty(*empty_keys):
    """
    Карточка, где пусты РОВНО перечисленные поля - остальные REQUIRED_PROJECT_FIELDS
    заполнены заглушкой. Просто {"Field": None} этого не даёт: is_filled(None)
    для отсутствующих ключей тоже False, то есть все незаписанные поля карточки
    молча попадают в "пустые" и раздувают широкий fallback-вопрос сверх ожидаемого.
    """
    from app.gaps import REQUIRED_PROJECT_FIELDS
    fields = {k: "filled" for k in REQUIRED_PROJECT_FIELDS}
    for key in empty_keys:
        fields[key] = None
    return fields


class TestUnknownFileClassificationFallback(unittest.TestCase):
    """
    Файлы, не опознанные по имени (implementation_plan.md, Э2, fallback), должны
    сначала спросить реестр (app/doc_classification_registry.py), затем при
    промахе классифицировать моделью и сохранить результат - а не сразу
    перебирать модель по каждому пустому полю подряд.
    """

    def _run(self, files, project_fields, cached_type=None, model_type=None,
              extract_side_effect=None, text="Some contract text"):
        extract_side_effect = extract_side_effect or (lambda t, field: _accepted(field))

        async def run_test():
            with patch('app.drive_folder.download_drive_file', return_value=True), \
                 patch('app.doc_pipeline._extract_text', new=AsyncMock(return_value=text)), \
                 patch('app.doc_pipeline.get_cached_classification',
                       new=AsyncMock(return_value=cached_type)), \
                 patch('app.doc_pipeline.classify_document_content',
                       new=AsyncMock(return_value=model_type)) as mock_classify, \
                 patch('app.doc_pipeline.save_classification',
                       new=AsyncMock()) as mock_save, \
                 patch('app.doc_pipeline.extract_field',
                       new=AsyncMock(side_effect=extract_side_effect)) as mock_extract:
                res = await fill_fields_from_drive_files(project_fields, files)
                res["_calls"] = [c.args[1] for c in mock_extract.await_args_list]
                res["_classify_called"] = mock_classify.await_count > 0
                res["_save_args"] = mock_save.await_args_list
                return res

        return asyncio.run(run_test())

    def test_cached_classification_skips_model_and_narrows_question(self):
        """Реестр уже знает тип - модель классификации не вызывается вовсе."""
        res = self._run(
            [_f("unnamed_scan_042.pdf", fid="drive-file-1")],
            _only_empty("Handover Permits", "Land Zoning Color"),
            cached_type="permits",
        )
        self.assertFalse(res["_classify_called"], "тип уже в реестре - модель классификации лишняя")
        self.assertEqual(res["_calls"], ["Handover Permits"],
                          "узкий вопрос только по полю, которое закрывает 'permits'")

    def test_uncached_file_is_classified_and_result_saved_to_registry(self):
        """
        Промах реестра -> классификация моделью -> запись в реестр, чтобы
        следующий прогон (этот или другой проект) не платил за тот же файл снова.
        """
        res = self._run(
            [_f("unnamed_scan_042.pdf", fid="drive-file-1")],
            _only_empty("Land Zoning Color"),
            cached_type=None,
            model_type="zoning",
        )
        self.assertTrue(res["_classify_called"])
        self.assertEqual(res["_calls"], ["Land Zoning Color"])
        self.assertEqual(len(res["_save_args"]), 1)
        args = res["_save_args"][0].args
        self.assertEqual(args[0], "drive-file-1")
        self.assertEqual(args[1], "zoning")
        self.assertEqual(res["_save_args"][0].kwargs.get("classified_by"), "model_fallback")

    def test_classified_type_irrelevant_to_gaps_skips_extraction_entirely(self):
        """
        Модель успешно определила тип документа, но он не про наши пустые поля -
        нет смысла тратить вызовы extract_field на заведомо не те вопросы.
        """
        res = self._run(
            [_f("unnamed_scan_042.pdf", fid="drive-file-1")],
            _only_empty("Handover Permits"),
            cached_type=None,
            model_type="pricing",
        )
        self.assertEqual(res["_calls"], [])
        self.assertTrue(any("classified as 'pricing'" in g for g in res["gaps"]))

    def test_genuinely_unclassifiable_file_falls_back_to_broad_questions(self):
        """
        Модель тоже не смогла классифицировать (None) - план разрешает
        последний резерв: спросить документ по всем ещё пустым полям, а не
        молча его пропустить.
        """
        res = self._run(
            [_f("unnamed_scan_042.pdf", fid="drive-file-1")],
            _only_empty("Handover Permits"),
            cached_type=None,
            model_type=None,
        )
        self.assertTrue(res["_classify_called"])
        self.assertEqual(res["_calls"], ["Handover Permits"])
        self.assertEqual(res["_save_args"], [], "нечего сохранять - классификация не удалась")

    def test_no_file_id_skips_registry_lookup_but_still_extracts(self):
        """Файл без id (теоретически) не должен падать - просто нет ключа для кэша."""
        res = self._run(
            [{"id": None, "name": "unnamed_scan_042.pdf", "mimeType": "application/pdf", "path": ""}],
            _only_empty("Handover Permits"),
            cached_type=None,
            model_type="permits",
        )
        self.assertEqual(res["_calls"], ["Handover Permits"])
        self.assertEqual(res["_save_args"], [], "без file_id сохранять некуда")


class TestCollectProjectLinks(unittest.TestCase):
    """Порядок обработки ссылок карточки и защита от повторов одного URL."""

    SHEET = "https://docs.google.com/spreadsheets/d/abc123/edit?gid=0#gid=0"
    FOLDER = "https://drive.google.com/drive/folders/xyz789"

    def test_sheet_is_processed_before_documents(self):
        """Владелец, 02.08.2026: шахматка - первый приоритет на сканирование."""
        links = collect_project_links({
            "Link to Developer’s Kit (Eng)": self.FOLDER,
            "Availability Chart": self.SHEET,
        })
        self.assertEqual([u for _, u in links], [self.SHEET, self.FOLDER])

    def test_same_url_in_two_fields_is_processed_once(self):
        """
        Регрессия 02.08.2026: у Mangata одна таблица указана и в DevKit (Rus),
        и в Availability Chart - извлечение полей отрабатывало по ней дважды и
        клало в Gaps два одинаковых предложения Land Zoning Color.
        """
        links = collect_project_links({
            "Link to Developer’s Kit (Rus)": self.SHEET,
            "Availability Chart": self.SHEET,
            "Link to Developer’s Kit (Eng)": self.FOLDER,
        })
        self.assertEqual([u for _, u in links], [self.SHEET, self.FOLDER])

    def test_non_http_and_empty_values_are_ignored(self):
        links = collect_project_links({
            "Link to Developer’s Kit (Rus)": "resale",
            "Availability Chart": "",
            "Link to Developer’s Kit (Eng)": self.FOLDER,
        })
        self.assertEqual([u for _, u in links], [self.FOLDER])


class TestFillProjectFieldsFromText(unittest.TestCase):
    """
    Владелец, 02.08.2026: «шахматка - первый приоритет на сканирование, из неё
    заполняется та часть полей, которую можно». Один текстовый источник целиком,
    без роутера и бюджета - в отличие от fill_fields_from_drive_files.
    """

    def _run(self, project_fields, text, extract_side_effect):
        async def run_test():
            with patch('app.doc_pipeline.extract_field',
                       new=AsyncMock(side_effect=extract_side_effect)) as mock_extract:
                res = await fill_project_fields_from_text(project_fields, text, "шахматка")
                res["_calls"] = sorted(c.args[1] for c in mock_extract.await_args_list)
                return res

        return asyncio.run(run_test())

    def test_asks_only_about_empty_fields(self):
        res = self._run(
            _only_empty("District", "Handover Date"),
            "Sanur, delivery July 2026",
            lambda t, f: _accepted(f, "Sanur" if f == "District" else "July 2026"),
        )
        self.assertEqual(res["_calls"], ["District", "Handover Date"])
        self.assertEqual(len(res["proposals"]), 2)
        self.assertEqual(res["opened"], 1)

    def test_accepted_and_rejected_fields_both_labelled(self):
        res = self._run(
            _only_empty("District", "Construction stage"),
            "table content",
            lambda t, f: _accepted(f) if f == "District" else _rejected(f),
        )
        self.assertEqual(len(res["proposals"]), 1)
        self.assertEqual(res["proposals"][0]["field"], "District")
        self.assertTrue(any("Construction stage" in g for g in res["gaps"]))

    def test_nothing_empty_means_no_model_call(self):
        from app.gaps import REQUIRED_PROJECT_FIELDS
        full = {k: "filled" for k in REQUIRED_PROJECT_FIELDS}
        res = self._run(full, "irrelevant text", lambda t, f: _accepted(f))
        self.assertEqual(res["_calls"], [])
        self.assertEqual(res["opened"], 0)

    def test_empty_text_means_no_model_call(self):
        res = self._run(_only_empty("District"), "   ", lambda t, f: _accepted(f))
        self.assertEqual(res["_calls"], [])
        self.assertEqual(res["opened"], 0)

    def test_proposal_carries_the_sheet_as_source(self):
        res = self._run(
            _only_empty("District"), "Sanur", lambda t, f: _accepted(f, "Sanur")
        )
        self.assertEqual(res["proposals"][0]["source_file"], "шахматка")


class TestGapsMerging(unittest.TestCase):
    """
    В Gaps уже лежат рукописные разборы (у Four Palms - отчёт с номерами
    регистраций и именами). Бот владеет только своей секцией между маркерами
    и не имеет права затирать текст человека.
    """

    HUMAN = ("MISSING - ask agent (Seacrest / @NabillaRauter):\n"
             "1. Handover Permits: intentionally left EMPTY. Evidence not conclusive.")

    def _summary(self, value="PBG"):
        return {"proposals": [{"field": "Handover Permits", "value": value,
                               "citation": "ok", "quotes": ["PBG issued"],
                               "needs_human": True, "source_file": "PBG.pdf"}],
                "gaps": ["Land Zoning Color: not stated"], "opened": 1}

    def test_human_text_survives_first_write(self):
        merged = merge_into_gaps(self.HUMAN, format_findings_block(self._summary()))
        self.assertIn("MISSING - ask agent", merged)
        self.assertIn("Handover Permits = PBG", merged)

    def test_repeated_run_replaces_only_bot_section(self):
        """Повторный прогон не плодит копии и не трогает текст человека."""
        first = merge_into_gaps(self.HUMAN, format_findings_block(self._summary("PBG")))
        second = merge_into_gaps(first, format_findings_block(self._summary("PBG/SLF")))

        self.assertEqual(second.count(GAPS_SECTION_START), 1)
        self.assertIn("MISSING - ask agent", second)
        self.assertIn("PBG/SLF", second)
        self.assertNotIn("Handover Permits = PBG\n", second.replace("PBG/SLF", "X"))

    def test_text_after_bot_section_is_preserved(self):
        existing = (self.HUMAN + "\n\n" + format_findings_block(self._summary())
                    + "\n\nЗаметка человека, дописанная ПОСЛЕ секции бота.")
        merged = merge_into_gaps(existing, format_findings_block(self._summary("X")))
        self.assertIn("дописанная ПОСЛЕ", merged)
        self.assertIn("MISSING - ask agent", merged)

    def test_unclosed_section_does_not_eat_the_rest(self):
        broken = self.HUMAN + "\n\n" + GAPS_SECTION_START + "\nоборванная секция"
        merged = merge_into_gaps(broken, format_findings_block(self._summary()))
        self.assertIn("MISSING - ask agent", merged)
        self.assertEqual(merged.count(GAPS_SECTION_START), 1)

    def test_proposals_are_labelled_as_proposals_not_values(self):
        """Человек должен видеть разницу между «записали» и «подтвердите»."""
        block = format_findings_block(self._summary())
        self.assertIn("в поля не записано", block)
        self.assertIn("только с Confirmed", block)

    def test_empty_result_still_writes_an_honest_line(self):
        block = format_findings_block({"proposals": [], "gaps": [], "opened": 0})
        self.assertIn("не дал ни предложений", block)

    def test_accepts_result_shape_from_process_generic_link(self):
        """
        Регрессия 02.08.2026: пакетный прогон отдаёт СЫРОЙ результат
        process_generic_link, где разбор лежит внутри 'doc_findings', а gaps
        уже домержены на верхний уровень. Писатель читал только верхний
        уровень, поэтому proposals превращались в [], а opened - в 0:
        46 карточек получили секцию AUTO без единого предложения.
        """
        docs = self._summary()
        outer = {
            "doc_findings": docs,
            "gaps": list(docs["gaps"]),          # link_fetcher домерживает их сюда
            "drive_mirror": {"dest_folder_id": "folder_1", "results": []},
        }
        block = format_findings_block(outer)

        self.assertIn("Handover Permits = PBG", block)
        self.assertIn("ПРЕДЛОЖЕНО", block)
        self.assertIn("(открыто документов: 1)", block)
        self.assertNotIn("не дал ни предложений", block)

    def test_flat_shape_still_works(self):
        """Плоскую форму run_for_project() ломать нельзя."""
        block = format_findings_block(self._summary())
        self.assertIn("Handover Permits = PBG", block)
        self.assertIn("(открыто документов: 1)", block)


class TestCombineFindings(unittest.TestCase):
    """
    Регрессия 02.08.2026 на Mangata: проект закрыт двумя ссылками (папка Drive
    с находками + таблица без doc_findings). save_findings_to_gaps по разу на
    ссылку означало, что вторая запись стирала находки первой - "открыто
    документов: 5" из папки исчезало без следа. Пакетные скрипты обязаны
    собрать результаты всех ссылок и записать один раз этой сводкой.
    """

    def test_merges_proposals_and_sums_opened_across_links(self):
        drive_folder_result = {
            "doc_findings": {
                "proposals": [{"field": "District", "value": "Sanur", "citation": "ok",
                               "quotes": [], "needs_human": False, "source_file": "brochure.pdf"}],
                "gaps": ["Handover Date: not stated"],
                "opened": 5,
            },
            "gaps": ["Handover Date: not stated"],
            "drive_mirror": {"dest_folder_id": "folder_1", "results": []},
        }
        sheet_result = {"gaps": [], "proposals": [], "opened": 0}

        combined = combine_findings([drive_folder_result, sheet_result])

        self.assertEqual(len(combined["proposals"]), 1)
        self.assertEqual(combined["proposals"][0]["field"], "District")
        self.assertEqual(combined["opened"], 5)
        self.assertEqual(combined["gaps"], ["Handover Date: not stated"])

    def test_deduplicates_identical_gaps_from_different_links(self):
        a = {"proposals": [], "gaps": ["Land Zoning Color: not stated"], "opened": 1}
        b = {"proposals": [], "gaps": ["Land Zoning Color: not stated"], "opened": 2}

        combined = combine_findings([a, b])

        self.assertEqual(combined["gaps"], ["Land Zoning Color: not stated"])
        self.assertEqual(combined["opened"], 3)

    def test_empty_results_list_is_safe(self):
        combined = combine_findings([])
        self.assertEqual(combined["proposals"], [])
        self.assertEqual(combined["opened"], 0)

    def test_first_source_wins_for_mirror_fields(self):
        a = {"drive_mirror": {"dest_folder_id": "folder_A", "results": []}}
        b = {"drive_mirror": {"dest_folder_id": "folder_B", "results": []}}

        combined = combine_findings([a, b])

        self.assertEqual(
            combined["mirror_airtable_fields"].get("Renders"),
            "https://drive.google.com/drive/folders/folder_A",
        )


class TestGapsWriteSafety(unittest.TestCase):

    def test_only_gaps_field_is_written(self):
        """Значения полей карточки не трогаются - они доезжают после Confirmed."""
        updates = {}

        class FakeTable:
            def get(self, rec_id):
                return {"id": rec_id, "fields": {"Gaps": "human text", "District": "Tabanan"}}

            def update(self, rec_id, fields):
                updates.update(fields)

        class FakeBase:
            def table(self, name):
                return FakeTable()

        async def run_test():
            with patch('app.airtable_client.get_base', return_value=FakeBase()):
                return await save_findings_to_gaps("rec1", {
                    "proposals": [{"field": "District", "value": "Badung", "citation": "ok",
                                   "quotes": [], "needs_human": False, "source_file": "x.pdf"}],
                    "gaps": [], "opened": 1,
                })

        asyncio.run(run_test())
        self.assertEqual(set(updates.keys()), {"Gaps"},
                         "запись должна касаться только Gaps")
        self.assertIn("human text", updates["Gaps"])


if __name__ == '__main__':
    unittest.main()
