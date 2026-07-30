import unittest
import asyncio
import os
import sys
from unittest.mock import patch, AsyncMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.link_fetcher import (
    extract_gsheet_id,
    extract_gdrive_id,
    is_notion_url,
    extract_nested_urls,
    process_generic_link
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


if __name__ == '__main__':
    unittest.main()
