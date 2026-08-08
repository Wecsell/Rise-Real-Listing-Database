import os
import sys
import asyncio
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import doc_parser


def _make_pdf(size_bytes: int = 64) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(b"%PDF-1.4" + b"\0" * max(0, size_bytes - 8))
    tmp.close()
    return tmp.name


class TestGraphicPdfPath(unittest.TestCase):
    """
    Регрессия: в doc_parser отсутствовал `import asyncio`, из-за чего ветка
    графического разбора PDF (скан без текстового слоя) всегда падала с
    NameError. Ошибку глотал широкий except, наружу уходило
    {"is_relevant": False, ...} — то есть Dev Kit'ы без текстового слоя
    молча не парсились никогда.

    Эта ветка теперь проверяет content_cache перед загрузкой в Gemini Files
    API (см. app/content_cache.py). _make_pdf() создает файл с одинаковыми
    байтами при каждом вызове, поэтому без изоляции кэш-БД второй прогон
    этого же теста (в этом же файле или в другом) получал бы попадание в
    реальный data/gemini_cache.db и mock upload переставал вызываться —
    тест ловил бы не регрессию, а собственный кэш.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = patch.dict(
            os.environ, {'GEMINI_CACHE_DB_PATH': os.path.join(self._tmpdir.name, 'cache.db')}
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_asyncio_is_importable_in_module(self):
        self.assertTrue(hasattr(doc_parser, 'asyncio'),
                        "doc_parser должен импортировать asyncio — его использует "
                        "asyncio.to_thread в ветке графического разбора")

    def test_pdf_without_text_layer_reaches_gemini(self):
        path = _make_pdf()
        try:
            fake_response = MagicMock()
            fake_response.text = '{"is_relevant": true, "confidence": 0.9}'

            fake_client = MagicMock()
            fake_client.files.upload.return_value = "uploaded-file-handle"
            fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)

            with patch.object(doc_parser, 'extract_text_from_pdf', return_value=''), \
                 patch.object(doc_parser, 'client', fake_client), \
                 patch.object(doc_parser, 'resolve_model_name', return_value='gemini-2.5-flash'):

                res = asyncio.run(doc_parser.parse_pdf_document(path))

            self.assertNotIn('error', res,
                             f"Графический PDF должен разбираться без ошибок, получено: {res}")
            self.assertTrue(res.get('is_relevant'))
            fake_client.files.upload.assert_called_once()
        finally:
            os.remove(path)


class TestTextLayerPath(unittest.TestCase):

    def test_pdf_with_text_layer_goes_through_parse_message(self):
        path = _make_pdf()
        try:
            long_text = "Проект Rise Villas Canggu. Цена от 250000 USD. " * 10
            with patch.object(doc_parser, 'extract_text_from_pdf', return_value=long_text), \
                 patch.object(doc_parser, 'parse_message',
                              new=AsyncMock(return_value={"is_relevant": True})) as mock_parse:

                res = asyncio.run(doc_parser.parse_pdf_document(path))

            self.assertTrue(res["is_relevant"])
            mock_parse.assert_awaited_once()
        finally:
            os.remove(path)


class TestOversizePdfFallback(unittest.TestCase):
    """PDF больше лимита Gemini (50 МБ) не должен уходить в Files API."""

    def test_oversize_pdf_without_text_records_gap(self):
        path = _make_pdf()
        try:
            fake_client = MagicMock()

            with patch.object(doc_parser, 'extract_text_from_pdf', return_value=''), \
                 patch.object(doc_parser.os.path, 'getsize', return_value=80 * 1024 * 1024), \
                 patch.object(doc_parser, 'client', fake_client):

                res = asyncio.run(doc_parser.parse_pdf_document(path))

            self.assertTrue(any('50MB' in g for g in res.get('Gaps', [])))
            fake_client.files.upload.assert_not_called()
        finally:
            os.remove(path)


class TestTextExtractionBackends(unittest.TestCase):
    """
    PyMuPDF - основной экстрактор, pypdf - запасной. Замер 03.08.2026 на
    реальной презентации Mangata: pypdf рвал разрядку шрифта ("ПРОГРЕ СС
    ~8 3%" вместо "ПРОГРЕСС ~83%"), из-за чего стадия готовности не
    находилась, а найденные значения не проходили сверку цитат.
    """

    def test_pymupdf_is_used_first(self):
        with patch.object(doc_parser, '_pages_via_pymupdf', return_value=["целый текст"]) as mupdf, \
             patch.object(doc_parser, '_pages_via_pypdf', return_value=["рва ный те кст"]) as pypdf_mock:
            out = doc_parser.extract_text_from_pdf("любой.pdf")

        mupdf.assert_called_once()
        pypdf_mock.assert_not_called()
        self.assertIn("целый текст", out)
        self.assertIn("--- Страница 1 ---", out)

    def test_falls_back_to_pypdf_when_pymupdf_unavailable(self):
        """Отсутствие PyMuPDF не должно терять документ целиком."""
        with patch.object(doc_parser, '_pages_via_pymupdf', side_effect=ImportError("no fitz")), \
             patch.object(doc_parser, '_pages_via_pypdf', return_value=["запасной текст"]):
            out = doc_parser.extract_text_from_pdf("любой.pdf")

        self.assertIn("запасной текст", out)

    def test_both_backends_failing_returns_empty_not_raises(self):
        with patch.object(doc_parser, '_pages_via_pymupdf', side_effect=RuntimeError("broken")), \
             patch.object(doc_parser, '_pages_via_pypdf', side_effect=RuntimeError("broken too")):
            self.assertEqual(doc_parser.extract_text_from_pdf("любой.pdf"), "")

    def test_blank_pages_are_not_numbered_into_output(self):
        with patch.object(doc_parser, '_pages_via_pymupdf', return_value=["первая", "   ", "третья"]):
            out = doc_parser.extract_text_from_pdf("любой.pdf")

        self.assertIn("--- Страница 1 ---", out)
        self.assertNotIn("--- Страница 2 ---", out)
        self.assertIn("--- Страница 3 ---", out)


if __name__ == '__main__':
    unittest.main()
