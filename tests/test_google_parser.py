import pytest
from app.google_parser import (
    convert_google_sheet_url_to_csv,
    convert_google_drive_url_to_direct_download,
)

class TestGoogleUrlConverters:
    """Тесты конвертации ссылок Google Drive и Google Sheets в экспортные ссылки."""

    def test_google_sheet_url_with_edit(self):
        url = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit#gid=0"
        expected = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/export?format=csv&gid=0"
        assert convert_google_sheet_url_to_csv(url) == expected

    def test_google_sheet_url_simple(self):
        url = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
        expected = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/export?format=csv"
        assert convert_google_sheet_url_to_csv(url) == expected

    def test_google_drive_file_view(self):
        url = "https://drive.google.com/file/d/1A2B3C4D5E6F7G8H9I0J/view?usp=sharing"
        expected = "https://drive.google.com/uc?export=download&id=1A2B3C4D5E6F7G8H9I0J"
        assert convert_google_drive_url_to_direct_download(url) == expected

    def test_invalid_google_urls(self):
        assert convert_google_sheet_url_to_csv("https://example.com") is None
        assert convert_google_drive_url_to_direct_download("https://example.com") is None
