import unittest
import asyncio
import os
import sys
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.link_fetcher import (
    extract_gsheet_id,
    extract_gdrive_id,
    is_notion_url,
    extract_nested_urls,
    process_generic_link,
    download_file_from_url,
    extract_notion_page_id,
    resolve_notion_page_id_from_html,
    fetch_notion_content,
    _notion_rich_text_to_text_and_links,
    _walk_notion_blocks,
)


class TestLinkFetcher(unittest.TestCase):

    def test_extract_gsheet_id(self):
        url = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit"
        sheet_id = extract_gsheet_id(url)
        self.assertEqual(sheet_id, "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms")

    def test_extract_gdrive_id(self):
        url = "https://drive.google.com/file/d/1abc123XYZ_test-id/view?usp=sharing"
        drive_id = extract_gdrive_id(url)
        self.assertEqual(drive_id, "1abc123XYZ_test-id")

    def test_notion_url_detection(self):
        self.assertTrue(is_notion_url("https://so.notion.site/Rise-Villas-Pererenan-12345"))
        self.assertTrue(is_notion_url("https://www.notion.so/my-workspace/Project-999"))
        self.assertFalse(is_notion_url("https://google.com"))

    def test_extract_nested_urls(self):
        text = """
        Вот презентация нашего объекта: https://so.notion.site/Project-Villa
        Скачать Dev Kit: https://drive.google.com/file/d/1xyz_devkit/view
        Шахматка юнитов: https://docs.google.com/spreadsheets/d/1sheet_id/edit
        Ссылка на сайт: https://example.com/info
        """
        urls = extract_nested_urls(text)
        self.assertIn("https://so.notion.site/Project-Villa", urls)
        self.assertIn("https://drive.google.com/file/d/1xyz_devkit/view", urls)
        self.assertIn("https://docs.google.com/spreadsheets/d/1sheet_id/edit", urls)
        self.assertNotIn("https://example.com/info", urls)


class TestPrivateLinkDetection(unittest.TestCase):
    """
    Раньше этот тест ходил в живой drive.google.com и проверял только
    assertIsNotNone(res) + assertIn("url", res) — то есть проходил при любом
    поведении функции, включая полностью сломанное. Теперь сеть замокана,
    а проверяется именно контракт: is_private, запись в gaps и вызов алерта.
    """

    def test_private_gdrive_link_sets_flag_and_gap(self):
        async def run_test():
            with patch('app.link_fetcher.download_file_from_url',
                       new=AsyncMock(return_value=(None, True))), \
                 patch('app.link_fetcher.notify_admin_private_link',
                       new=AsyncMock()) as mock_alert:

                res = await process_generic_link(
                    "https://drive.google.com/file/d/private_file_id/view"
                )

                self.assertTrue(res["is_private"])
                self.assertTrue(any("Private" in g for g in res["gaps"]))
                mock_alert.assert_awaited_once()

        asyncio.run(run_test())

    def test_public_gdrive_pdf_is_parsed_and_temp_file_removed(self):
        """Успешный путь: файл скачался, распарсился, временный файл удален."""
        async def run_test():
            import tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(b"%PDF-1.4")
            tmp.close()

            with patch('app.link_fetcher.download_file_from_url',
                       new=AsyncMock(return_value=(tmp.name, False))), \
                 patch('app.link_fetcher.parse_pdf_document',
                       new=AsyncMock(return_value={"is_relevant": True})) as mock_parse:

                res = await process_generic_link(
                    "https://drive.google.com/file/d/public_file_id/view"
                )

                self.assertFalse(res["is_private"])
                self.assertEqual(res["parsed_data"], {"is_relevant": True})
                mock_parse.assert_awaited_once()
                self.assertFalse(os.path.exists(tmp.name),
                                 "Временный файл должен удаляться после разбора")

        asyncio.run(run_test())


class TestVirusScanInterstitialIsDetected(unittest.TestCase):
    """
    Для файлов, слишком больших для антивирусной проверки (порог ~100 МБ),
    Google Drive возвращает не сам файл, а HTML-страницу подтверждения со
    статусом 200 ("Google Drive can't scan this file for viruses...").
    download_file_from_url() отличает провал только по 401/403/ServiceLogin -
    статус 200 безусловно считается успехом, и эта HTML-страница сохраняется
    как ".pdf", после чего пайплайн пытается её распарсить как настоящий
    документ вместо того, чтобы явно сообщить "файл слишком большой".
    Найдено при анализе экономики (2026-08-01) после разбора реального
    16.5 МБ мастер-лиза Four Palms - под порог не попал, но структурного
    предохранителя от этого сценария в коде нет вообще.
    """

    INTERSTITIAL_HTML = (
        b"<html><body>Google Drive can't scan this file for viruses.<br>"
        b"<a href='...'>Download anyway</a></body></html>"
    )

    def test_interstitial_page_is_not_treated_as_downloaded_pdf(self):
        async def run_test():
            class FakeResponse:
                status_code = 200
                url = "https://drive.usercontent.google.com/download?id=big_file&confirm=1"
                content = TestVirusScanInterstitialIsDetected.INTERSTITIAL_HTML

            class FakeAsyncClient:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return False
                async def get(self, url):
                    return FakeResponse()

            with patch('app.link_fetcher.httpx.AsyncClient', return_value=FakeAsyncClient()):
                file_path, is_private = await download_file_from_url(
                    "https://docs.google.com/uc?export=download&id=big_file", suffix=".pdf"
                )

            self.assertIsNone(
                file_path,
                "HTML-интерстишл сохранён как будто это настоящий PDF - "
                "парсер получит на вход мусор без единого сигнала об этом"
            )

        asyncio.run(run_test())


class TestParsedResultIsPersisted(unittest.TestCase):
    """
    process_generic_link() возвращает parsed_data вызывающему коду, но ни
    listener.py, ни history_scanner.py это значение никуда не сохраняют -
    оно просто выбрасывается. Единственный источник, который сохраняется
    сам, это Google Sheets (fetch_and_parse_link пишет save_extraction
    изнутри). Для Drive PDF и Notion результат парсинга должен сохраняться
    той же функцией, что использует остальной проект, иначе даже полностью
    рабочий парсинг не долетает до базы.
    """

    def test_drive_pdf_result_is_saved(self):
        async def run_test():
            import tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(b"%PDF-1.4")
            tmp.close()

            parsed = {
                "is_relevant": True,
                "confidence": 0.9,
                "reason": "test",
                "Projects": {"Project Name": "Test Villa"},
            }

            with patch('app.link_fetcher.download_file_from_url',
                       new=AsyncMock(return_value=(tmp.name, False))), \
                 patch('app.link_fetcher.parse_pdf_document',
                       new=AsyncMock(return_value=parsed)), \
                 patch('app.link_fetcher.save_extraction',
                       new=AsyncMock()) as mock_save:

                await process_generic_link(
                    "https://drive.google.com/file/d/public_file_id/view",
                    message_id=111, chat_id=222,
                )

                mock_save.assert_awaited_once()
                _, kwargs = mock_save.call_args
                self.assertEqual(kwargs["message_id"], 111)
                self.assertEqual(kwargs["chat_id"], 222)
                self.assertEqual(kwargs["project_recid"], "Test Villa")
                self.assertEqual(kwargs["raw_json"], parsed)

        asyncio.run(run_test())

    def test_notion_result_is_saved(self):
        async def run_test():
            parsed = {
                "is_relevant": True,
                "confidence": 0.85,
                "reason": "test",
                "Projects": {"Project Name": "Four Palms Villas"},
            }
            long_text = "Four Palms Villas private 2-bedroom villas " * 5

            with patch('app.link_fetcher.fetch_notion_content',
                       new=AsyncMock(return_value=(long_text, [], False))), \
                 patch('app.gemini_parser.parse_message',
                       new=AsyncMock(return_value=parsed)), \
                 patch('app.link_fetcher.save_extraction',
                       new=AsyncMock()) as mock_save:

                await process_generic_link(
                    "https://fourpalmsvillaskedungu.notion.site/?pvs=73",
                    message_id=333, chat_id=444,
                )

                mock_save.assert_awaited_once()
                _, kwargs = mock_save.call_args
                self.assertEqual(kwargs["project_recid"], "Four Palms Villas")
                self.assertEqual(kwargs["raw_json"], parsed)

        asyncio.run(run_test())

    def test_irrelevant_result_is_not_saved(self):
        """is_relevant=False не должен создавать запись - как и в listener.py."""
        async def run_test():
            import tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(b"%PDF-1.4")
            tmp.close()

            with patch('app.link_fetcher.download_file_from_url',
                       new=AsyncMock(return_value=(tmp.name, False))), \
                 patch('app.link_fetcher.parse_pdf_document',
                       new=AsyncMock(return_value={"is_relevant": False, "reason": "noise"})), \
                 patch('app.link_fetcher.save_extraction',
                       new=AsyncMock()) as mock_save:

                await process_generic_link(
                    "https://drive.google.com/file/d/public_file_id/view",
                    message_id=1, chat_id=2,
                )

                mock_save.assert_not_awaited()

        asyncio.run(run_test())


class TestRecursionGuards(unittest.TestCase):
    """Защита от циклов и разрастания рекурсии по Notion."""

    def test_already_visited_url_is_skipped(self):
        async def run_test():
            visited = {"https://so.notion.site/Page-A"}
            res = await process_generic_link(
                "https://so.notion.site/Page-A", visited=visited
            )
            self.assertIn("Cycle or max depth reached", res["gaps"])

        asyncio.run(run_test())

    def test_depth_over_limit_is_skipped(self):
        async def run_test():
            res = await process_generic_link(
                "https://so.notion.site/Deep-Page", depth=3, max_depth=2
            )
            self.assertIn("Cycle or max depth reached", res["gaps"])

        asyncio.run(run_test())


class TestDriveFolderBranchWiring(unittest.TestCase):
    """
    Ветка "Google Drive File or Folder" для папок раньше была заглушкой -
    помечала dev_kit_url и выходила, ни один файл внутри не читался
    (implementation_plan.md, Э1). Теперь она реально вызывает листинг, но
    должна переживать три сценария без падения всего пайплайна: успех,
    ещё не настроенный OAuth (ожидаемо на проде до Э6), и любую другую ошибку API.
    """

    def test_folder_listing_success_populates_drive_files(self):
        async def run_test():
            files = [{"id": "f1", "name": "brochure.pdf", "mimeType": "application/pdf"}]
            with patch('app.drive_folder.list_drive_folder_recursive', return_value=files):
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link"
                )
            self.assertEqual(res["drive_files"], files)
            self.assertEqual(res["gaps"], [])

        asyncio.run(run_test())

    def test_missing_oauth_token_is_a_gap_not_a_crash(self):
        """
        До того как владелец один раз запустит tools_drive_auth_setup.py,
        get_drive_service() бросает RuntimeError - это ожидаемое состояние
        на проде до реализации Э6, не повод ронять обработку сообщения.
        """
        async def run_test():
            with patch('app.drive_folder.list_drive_folder_recursive',
                       side_effect=RuntimeError("Токен Google Drive не найден...")):
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link"
                )
            self.assertIsNone(res["drive_files"])
            self.assertTrue(any("folder listing unavailable" in g for g in res["gaps"]))

        asyncio.run(run_test())

    def test_unexpected_api_error_is_a_gap_not_a_crash(self):
        async def run_test():
            with patch('app.drive_folder.list_drive_folder_recursive',
                       side_effect=Exception("boom")):
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link"
                )
            self.assertIsNone(res["drive_files"])
            self.assertTrue(any("folder listing failed" in g for g in res["gaps"]))

        asyncio.run(run_test())


class TestDriveFolderMirrorWiring(unittest.TestCase):
    """
    Э6: найденные в папке изображения зеркалируются на личный Drive владельца,
    но только когда вызывающий код знает имя проекта - без него зеркалировать
    некуда (implementation_plan.md, Э6 "проводка").
    """

    def test_mirror_not_triggered_without_project_name(self):
        async def run_test():
            files = [{"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}]
            with patch('app.drive_folder.list_drive_folder_recursive', return_value=files), \
                 patch('app.drive_mirror.mirror_listed_drive_folder') as mock_mirror:
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link"
                )
            mock_mirror.assert_not_called()
            self.assertNotIn("drive_mirror", res)

        asyncio.run(run_test())

    def test_mirror_triggered_with_project_name(self):
        async def run_test():
            files = [{"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}]
            summary = {"dest_folder_id": "dest", "results": [{"status": "copied", "name": "render.jpg"}], "gaps": []}
            with patch('app.drive_folder.list_drive_folder_recursive', return_value=files), \
                 patch('app.drive_mirror.mirror_listed_drive_folder', return_value=summary) as mock_mirror:
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link",
                    project_name="Four Palms Villas",
                )
            mock_mirror.assert_called_once_with("abc123", files, "Four Palms Villas")
            self.assertEqual(res["drive_mirror"], summary)

        asyncio.run(run_test())

    def test_mirror_gaps_are_merged_into_result_gaps(self):
        async def run_test():
            files = [{"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}]
            summary = {"dest_folder_id": "dest", "results": [], "gaps": ["Drive mirror failed for render.jpg: cannotCopyFile"]}
            with patch('app.drive_folder.list_drive_folder_recursive', return_value=files), \
                 patch('app.drive_mirror.mirror_listed_drive_folder', return_value=summary):
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link",
                    project_name="Four Palms Villas",
                )
            self.assertIn("Drive mirror failed for render.jpg: cannotCopyFile", res["gaps"])

        asyncio.run(run_test())

    def test_mirror_oauth_not_configured_is_a_gap_not_a_crash(self):
        """Тот же ожидаемый RuntimeError, что и у листинга - не должен ронять всю ссылку."""
        async def run_test():
            files = [{"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}]
            with patch('app.drive_folder.list_drive_folder_recursive', return_value=files), \
                 patch('app.drive_mirror.mirror_listed_drive_folder',
                       side_effect=RuntimeError("Токен Google Drive не найден...")):
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link",
                    project_name="Four Palms Villas",
                )
            self.assertTrue(any("mirror unavailable" in g for g in res["gaps"]))

        asyncio.run(run_test())

    def test_mirror_unexpected_error_is_a_gap_not_a_crash(self):
        async def run_test():
            files = [{"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}]
            with patch('app.drive_folder.list_drive_folder_recursive', return_value=files), \
                 patch('app.drive_mirror.mirror_listed_drive_folder', side_effect=Exception("boom")):
                res = await process_generic_link(
                    "https://drive.google.com/drive/folders/abc123?usp=share_link",
                    project_name="Four Palms Villas",
                )
            self.assertTrue(any("mirror failed" in g for g in res["gaps"]))

        asyncio.run(run_test())

    def test_project_name_propagates_to_nested_notion_drive_links(self):
        """
        Ссылки на Drive-папки внутри Notion (nested_urls) должны получать то же
        project_name, что и сама Notion-страница - иначе зеркалирование сработает
        только на верхнем уровне обхода, а реальные папки рендеров лежат внутри.
        """
        async def run_test():
            summary = {"dest_folder_id": "dest", "results": [], "gaps": []}
            with patch('app.link_fetcher.fetch_notion_content',
                       new=AsyncMock(return_value=(
                           "some project text " * 10,
                           ["https://drive.google.com/drive/folders/nested123"],
                           False,
                       ))), \
                 patch('app.gemini_parser.parse_message', new=AsyncMock(return_value={"is_relevant": True})), \
                 patch('app.drive_folder.list_drive_folder_recursive', return_value=[]), \
                 patch('app.drive_mirror.mirror_listed_drive_folder', return_value=summary) as mock_mirror:
                await process_generic_link(
                    "https://fourpalmsvillaskedungu.notion.site/Four-Palms-375c12df405980d4bf7ed1e05544f6d9",
                    project_name="Four Palms Villas",
                )
            mock_mirror.assert_called_once_with("nested123", [], "Four Palms Villas")

        asyncio.run(run_test())


class TestDocParsingWiring(unittest.TestCase):
    """
    Э2 в пайплайне: найденные в папке документы разбираются под пустые поля
    карточки. Предложения никуда не пишутся - их судьбу решает Confirmed.
    """

    URL = "https://drive.google.com/drive/folders/abc123?usp=share_link"

    def _run(self, project_name, run_for_project_mock):
        async def run_test():
            files = [{"id": "f1", "name": "PBG.pdf", "mimeType": "application/pdf"}]
            with patch('app.drive_folder.list_drive_folder_recursive', return_value=files), \
                 patch('app.drive_mirror.mirror_listed_drive_folder',
                       return_value={"dest_folder_id": "d", "results": [], "gaps": []}), \
                 patch('app.doc_pipeline.run_for_project', new=run_for_project_mock):
                return await process_generic_link(self.URL, project_name=project_name)

        return asyncio.run(run_test())

    def test_findings_are_attached_and_gaps_merged(self):
        summary = {"proposals": [{"field": "Handover Permits", "value": "PBG"}],
                   "gaps": ["Land Zoning Color (ITR.pdf): citation not found in source"],
                   "opened": 1}
        res = self._run("Four Palms Villas", AsyncMock(return_value=summary))
        self.assertEqual(res["doc_findings"], summary)
        self.assertIn("Land Zoning Color (ITR.pdf): citation not found in source", res["gaps"])

    def test_project_not_in_base_means_no_parsing(self):
        """Открывать документы, не зная чего не хватает, план запрещает прямо."""
        res = self._run("Unknown Project", AsyncMock(return_value=None))
        self.assertNotIn("doc_findings", res)

    def test_parsing_error_is_a_gap_not_a_crash(self):
        res = self._run("Four Palms Villas", AsyncMock(side_effect=Exception("boom")))
        self.assertTrue(any("Document parsing failed" in g for g in res["gaps"]))

    def test_no_parsing_without_project_name(self):
        mock = AsyncMock()
        self._run(None, mock)
        mock.assert_not_awaited()


class TestNotionPageIdExtraction(unittest.TestCase):
    """
    extract_notion_page_id() - фундамент чтения Notion через внутренний API:
    без id страницы запрос к loadCachedPageChunkV2 сделать нечем.
    """

    def test_id_extracted_and_dashed(self):
        url = "https://fourpalmsvillaskedungu.notion.site/Kedungu-Beach-375c12df405980d4bf7ed1e05544f6d9?pvs=25"
        self.assertEqual(
            extract_notion_page_id(url), "375c12df-4059-80d4-bf7e-d1e05544f6d9"
        )

    def test_bare_root_link_has_no_id(self):
        """
        Голая корневая ссылка ("domain.notion.site/?pvs=73") - реальный случай
        из Field Staging #55 (Four Palms). Её HTML не содержит ни одного UUID
        (проверено вручную), извлечь id из URL тоже нечего.
        """
        self.assertIsNone(
            extract_notion_page_id("https://fourpalmsvillaskedungu.notion.site/?pvs=73")
        )

    def test_id_with_query_string_after_it(self):
        url = "https://x.notion.site/Page-375c12df405980d4bf7ed1e05544f6d9?v=abc&pvs=18"
        self.assertEqual(
            extract_notion_page_id(url), "375c12df-4059-80d4-bf7e-d1e05544f6d9"
        )


class TestNotionRichText(unittest.TestCase):
    """Формат rich-text Notion: [[текст, [[метка, ...]]], ...]. Метка 'a' - ссылка."""

    def test_plain_text_no_links(self):
        rt = [["Four Palms Villas"]]
        text, links = _notion_rich_text_to_text_and_links(rt)
        self.assertEqual(text, "Four Palms Villas")
        self.assertEqual(links, [])

    def test_bold_mark_is_not_mistaken_for_link(self):
        rt = [["Want current-stage pricing", [["b"]]]]
        text, links = _notion_rich_text_to_text_and_links(rt)
        self.assertEqual(text, "Want current-stage pricing")
        self.assertEqual(links, [])

    def test_link_mark_extracted(self):
        # реальная форма чанка из Four Palms ("Brochures" -> папка Drive)
        rt = [["📑 "], ["Brochures", [["a", "https://drive.google.com/drive/folders/ABC"]]]]
        text, links = _notion_rich_text_to_text_and_links(rt)
        self.assertEqual(text, "📑 Brochures")
        self.assertEqual(links, ["https://drive.google.com/drive/folders/ABC"])

    def test_empty_rich_text(self):
        text, links = _notion_rich_text_to_text_and_links(None)
        self.assertEqual((text, links), ("", []))


class TestNotionBlockTreeWalk(unittest.TestCase):
    """
    _walk_notion_blocks() обходит дерево в порядке чтения (через content[]),
    а не в порядке ключей словаря блоков - иначе текст для модели придёт
    вперемешку и потеряет смысл как связный документ.
    """

    def _mkblocks(self, tree):
        """tree: {id: (properties_dict, [child_ids])} -> формат recordMap.block"""
        return {
            bid: {"value": {"value": {"properties": props, "content": children}}}
            for bid, (props, children) in tree.items()
        }

    def test_depth_first_order_preserved(self):
        blocks = self._mkblocks({
            "root": ({"title": [["Root"]]}, ["child1", "child2"]),
            "child1": ({"title": [["First"]]}, []),
            "child2": ({"title": [["Second"]]}, []),
        })
        text, links = _walk_notion_blocks("root", blocks)
        self.assertEqual(text, "Root\nFirst\nSecond")
        self.assertEqual(links, [])

    def test_links_collected_from_nested_children(self):
        blocks = self._mkblocks({
            "root": ({"title": [["Docs"]]}, ["a", "b"]),
            "a": ({"title": [["Brochures", [["a", "https://drive.google.com/x"]]]]}, []),
            "b": ({"title": [["Legal", [["a", "https://drive.google.com/y"]]]]}, []),
        })
        text, links = _walk_notion_blocks("root", blocks)
        self.assertEqual(links, ["https://drive.google.com/x", "https://drive.google.com/y"])

    def test_missing_child_id_does_not_crash(self):
        """Ссылка в content[] на блок, которого нет в ответе (не пришёл с сервера)."""
        blocks = self._mkblocks({"root": ({"title": [["Root"]]}, ["ghost"])})
        text, links = _walk_notion_blocks("root", blocks)
        self.assertEqual(text, "Root")

    def test_cycle_does_not_infinite_loop(self):
        blocks = self._mkblocks({
            "a": ({"title": [["A"]]}, ["b"]),
            "b": ({"title": [["B"]]}, ["a"]),
        })
        text, links = _walk_notion_blocks("a", blocks)
        self.assertEqual(text, "A\nB")

    def test_table_row_text_collected_from_arbitrary_property_keys(self):
        """
        table_row хранит ячейки под случайными id колонок ('K~]U'), не под 'title' -
        реальный формат из шахматки цен Four Palms. Текст должен собираться из
        любых свойств блока, не только из 'title'.
        """
        blocks = self._mkblocks({
            "row": ({"K~]U": [["Villa 1"]], "z^D@": [["135 sqm"]]}, []),
        })
        text, _ = _walk_notion_blocks("row", blocks)
        self.assertIn("Villa 1", text)
        self.assertIn("135 sqm", text)


class TestFetchNotionContentPagination(unittest.TestCase):
    """
    Регрессия на главную находку 2026-08-01: cursor:null в ответе Notion НЕ
    означает "всё загружено". Первый вызов loadCachedPageChunkV2 на реальной
    странице Four Palms вернул cursor:null, но 16 из 41 прямых потомков
    отсутствовали - включая все ссылки на Google Drive (Brochures, Legal и
    т.п.). Полное дерево пришло только после прохода cursor.stack по
    нарастающему index. Мокаем ровно это поведение: первый вызов - неполный
    ответ с cursor:null, второй - недостающий блок со ссылкой.
    """

    ROOT_ID = "375c12df-4059-80d9-bc3a-c1dcf864df24"

    def _block(self, title_rt, content=None):
        return {"value": {"value": {"properties": {"title": title_rt}, "content": content or []}}}

    def test_incomplete_first_page_is_completed_by_pagination(self):
        async def run_test():
            first_response = {
                "spaceId": "space-1",
                "cursor": None,  # лживый сигнал "готово" - в реальности не всё пришло
                "recordMap": {"block": {
                    self.ROOT_ID: self._block([["Four Palms Villas"]], content=["missing_link_block"]),
                }},
            }
            second_response = {
                "spaceId": "space-1",
                "cursor": None,
                "recordMap": {"block": {
                    "missing_link_block": self._block(
                        [["📑 "], ["Brochures", [["a", "https://drive.google.com/drive/folders/ABC"]]]]
                    ),
                }},
            }
            third_response_empty = {"spaceId": "space-1", "cursor": None, "recordMap": {"block": {}}}

            call_log = []

            class FakeResponse:
                def __init__(self, payload):
                    self.status_code = 200
                    self._payload = payload
                def json(self):
                    return self._payload

            class FakeAsyncClient:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return False
                async def post(self, url, json=None):
                    call_log.append(json)
                    if len(call_log) == 1:
                        return FakeResponse(first_response)
                    if len(call_log) == 2:
                        return FakeResponse(second_response)
                    return FakeResponse(third_response_empty)

            with patch('app.link_fetcher.httpx.AsyncClient', return_value=FakeAsyncClient()):
                text, nested, is_private = await fetch_notion_content(
                    f"https://fourpalmsvillaskedungu.notion.site/Four-Palms-{self.ROOT_ID.replace('-', '')}"
                )

            self.assertFalse(is_private)
            self.assertIn("Four Palms Villas", text)
            self.assertIn("Brochures", text)
            self.assertEqual(nested, ["https://drive.google.com/drive/folders/ABC"])
            # первый вызов обязан был использовать пустой cursor.stack (index 0)
            self.assertEqual(call_log[0]["cursor"], {"stack": []})
            # раз новых блоков не пришло - обход должен был остановиться,
            # не продолжаясь до отметки index=400
            self.assertLessEqual(len(call_log), 5)

        asyncio.run(run_test())

    def test_bare_root_link_returns_no_content_without_crashing(self):
        """
        По-настоящему голая корневая ссылка: id нет ни в пути, ни в HTML
        (там только общая для всех страниц константа Notion). Должна честно
        вернуть пустоту, а не упасть и не выдать мусор.
        """
        async def run_test():
            with patch('app.link_fetcher.resolve_notion_page_id_from_html',
                       new=AsyncMock(return_value=None)):
                text, nested, is_private = await fetch_notion_content(
                    "https://fourpalmsvillaskedungu.notion.site/?pvs=73"
                )
            self.assertIsNone(text)
            self.assertEqual(nested, [])
            self.assertFalse(is_private)

        asyncio.run(run_test())

    def test_slug_link_resolves_page_id_from_html(self):
        """
        Ссылка со slug вместо id (domain.notion.site/elysiumgroupbali) - таких
        в живой базе большинство среди нерезолвящихся. Раньше отбрасывалась
        как безнадёжная вместе с голой корневой; на деле её id лежит в HTML.
        """
        async def run_test():
            with patch('app.link_fetcher.resolve_notion_page_id_from_html',
                       new=AsyncMock(return_value="23a783ac-d377-8053-8b3c-d53723ed054c")) as mock_resolve, \
                 patch('app.link_fetcher._fetch_notion_block_tree',
                       new=AsyncMock(return_value={
                           "23a783ac-d377-8053-8b3c-d53723ed054c": {
                               "value": {"value": {
                                   "id": "23a783ac-d377-8053-8b3c-d53723ed054c",
                                   "type": "page",
                                   "properties": {"title": [["Elysium Group"]]},
                                   "content": [],
                               }}
                           }
                       })):
                text, nested, is_private = await fetch_notion_content(
                    "https://checkered-twister-bc5.notion.site/elysiumgroupbali"
                )
            mock_resolve.assert_awaited_once()
            self.assertIn("Elysium Group", text)
            self.assertFalse(is_private)

        asyncio.run(run_test())


class TestNotionPageIdFromHtml(unittest.TestCase):
    """
    resolve_notion_page_id_from_html(): HTML любой страницы Notion содержит одну
    и ту же константу-UUID (проверено на четырёх разных сайтах застройщиков).
    Настоящий id страницы - это второй, уникальный UUID; если его нет, ссылка
    действительно нерезолвима без браузера.
    """

    CONST = "EA76605A-F565-4B17-A496-34435622A1EB"

    def _html(self, *uuids):
        return "<html>" + " ".join(f'"{u}"' for u in uuids) + "</html>"

    def _run_with_html(self, html, status=200):
        async def run_test():
            fake_res = MagicMock(status_code=status, text=html)
            fake_client = MagicMock()
            fake_client.get = AsyncMock(return_value=fake_res)
            fake_client.__aenter__ = AsyncMock(return_value=fake_client)
            fake_client.__aexit__ = AsyncMock(return_value=False)
            with patch('httpx.AsyncClient', return_value=fake_client):
                return await resolve_notion_page_id_from_html("https://x.notion.site/slug")

        return asyncio.run(run_test())

    def test_unique_uuid_is_returned(self):
        real = "23a783ac-d377-8053-8b3c-d53723ed054c"
        self.assertEqual(self._run_with_html(self._html(self.CONST, real)), real)

    def test_constant_only_means_unresolvable(self):
        """Голая корневая: кроме константы UUID нет - id взять неоткуда."""
        self.assertIsNone(self._run_with_html(self._html(self.CONST)))

    def test_constant_is_not_returned_even_if_it_comes_first(self):
        real = "23898b94-160e-80f6-be4d-c696dee46d7f"
        self.assertNotEqual(self._run_with_html(self._html(self.CONST, real)), self.CONST.lower())

    def test_http_error_returns_none_not_crash(self):
        self.assertIsNone(self._run_with_html("<html></html>", status=404))


class TestAllNestedNotionLinksAreVisited(unittest.TestCase):
    """
    Регрессия: раньше цикл обхода вложенных Notion-ссылок содержал break после
    первой подошедшей ссылки. Это было незаметно, пока Notion парсился в
    пустышку (nested_urls всегда были []). После починки чтения Notion
    страница отдаёт реально до 7+ ссылок на папки Drive - без фикса break
    отбросил бы все, кроме первой.
    """

    def test_all_drive_folder_links_are_processed_not_just_first(self):
        async def run_test():
            nested = [
                "https://drive.google.com/drive/folders/folder1",
                "https://drive.google.com/drive/folders/folder2",
                "https://drive.google.com/drive/folders/folder3",
            ]
            visited = set()  # передаём свой set, чтобы заглянуть в него после вызова

            with patch('app.link_fetcher.fetch_notion_content',
                       new=AsyncMock(return_value=("Four Palms Villas description text here", nested, False))):
                await process_generic_link(
                    "https://fourpalmsvillaskedungu.notion.site/?pvs=73",
                    message_id=1, chat_id=2, visited=visited,
                )

            # process_generic_link добавляет url_clean в visited в самом начале
            # каждого своего вызова (включая рекурсивные) - значит, если ссылка
            # реально была посещена, она окажется в этом же объекте set.
            # С прежним break здесь оказалась бы только folder1.
            for url in nested:
                self.assertIn(url, visited, f"{url} не был посещён - действует ли ещё break?")

        asyncio.run(run_test())


if __name__ == '__main__':
    unittest.main()
