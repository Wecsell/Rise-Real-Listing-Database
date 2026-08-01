import unittest
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.drive_folder import list_drive_folder_recursive


def _fake_service(folder_children: dict):
    """
    folder_children: {folder_id: [file_dict, ...]} - имитирует files().list()
    без пагинации (у наших тестовых папок меньше 100 файлов).
    """
    service = MagicMock()

    def list_side_effect(q, **kwargs):
        folder_id = q.split("'")[1]
        exec_mock = MagicMock()
        exec_mock.execute.return_value = {"files": folder_children.get(folder_id, [])}
        return exec_mock

    service.files.return_value.list.side_effect = list_side_effect
    return service


class TestDriveFolderListing(unittest.TestCase):

    @patch('app.drive_folder.get_drive_service')
    def test_flat_folder_lists_files(self, mock_get_service):
        mock_get_service.return_value = _fake_service({
            "root": [
                {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf", "size": "100"},
                {"id": "f2", "name": "b.jpg", "mimeType": "image/jpeg", "size": "200"},
            ]
        })
        result = list_drive_folder_recursive("root")
        names = {r["name"] for r in result}
        self.assertEqual(names, {"a.pdf", "b.jpg"})

    @patch('app.drive_folder.get_drive_service')
    def test_nested_subfolder_is_expanded(self, mock_get_service):
        mock_get_service.return_value = _fake_service({
            "root": [
                {"id": "sub", "name": "Legal", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "f1", "name": "top.pdf", "mimeType": "application/pdf"},
            ],
            "sub": [
                {"id": "f2", "name": "deed.pdf", "mimeType": "application/pdf"},
            ],
        })
        result = list_drive_folder_recursive("root")
        names = {r["name"] for r in result}
        self.assertEqual(names, {"top.pdf", "deed.pdf"})

    @patch('app.drive_folder.get_drive_service')
    def test_shortcut_to_folder_is_dereferenced(self, mock_get_service):
        """
        Реальный случай из Legal-папки Four Palms: "Company renamed - PT
        SEACREST REAL ESTATE Shortcut to Shared folder". Ярлык копирует
        указатель, а не содержимое - без разыменования листинг вернул бы
        пустышку вместо реальных файлов компании.
        """
        mock_get_service.return_value = _fake_service({
            "root": [{
                "id": "shortcut1", "name": "Company Shortcut",
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {
                    "targetId": "real_folder",
                    "targetMimeType": "application/vnd.google-apps.folder",
                },
            }],
            "real_folder": [
                {"id": "f1", "name": "akta.pdf", "mimeType": "application/pdf"},
            ],
        })
        result = list_drive_folder_recursive("root")
        self.assertEqual([r["name"] for r in result], ["akta.pdf"])

    @patch('app.drive_folder.get_drive_service')
    def test_shortcut_to_file_is_dereferenced(self, mock_get_service):
        mock_get_service.return_value = _fake_service({
            "root": [{
                "id": "shortcut1", "name": "Passport Shortcut",
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {
                    "targetId": "real_file",
                    "targetMimeType": "image/jpeg",
                },
            }],
        })
        result = list_drive_folder_recursive("root")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "real_file")
        self.assertEqual(result[0]["mimeType"], "image/jpeg")

    @patch('app.drive_folder.get_drive_service')
    def test_folder_cycle_via_shortcuts_does_not_infinite_loop(self, mock_get_service):
        """Папка А содержит ярлык на папку Б, папка Б - ярлык обратно на А."""
        mock_get_service.return_value = _fake_service({
            "A": [{
                "id": "s_to_b", "name": "-> B",
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {"targetId": "B", "targetMimeType": "application/vnd.google-apps.folder"},
            }],
            "B": [{
                "id": "s_to_a", "name": "-> A",
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {"targetId": "A", "targetMimeType": "application/vnd.google-apps.folder"},
            }],
        })
        result = list_drive_folder_recursive("A", max_depth=10)
        self.assertEqual(result, [])  # ни одного реального файла, цикл не завис

    @patch('app.drive_folder.get_drive_service')
    def test_depth_limit_is_respected(self, mock_get_service):
        chain = {}
        for i in range(10):
            chain[f"id{i}"] = [{
                "id": f"id{i+1}", "name": f"level{i+1}",
                "mimeType": "application/vnd.google-apps.folder",
            }]
        mock_get_service.return_value = _fake_service(chain)

        result = list_drive_folder_recursive("id0", max_depth=3)
        self.assertEqual(result, [])  # только папки до упора глубины, файлов нет нигде

    @patch('app.drive_folder.get_drive_service')
    def test_http_error_returns_empty_list_not_crash(self, mock_get_service):
        from googleapiclient.errors import HttpError
        service = MagicMock()
        fake_resp = MagicMock(status=403)
        service.files.return_value.list.return_value.execute.side_effect = HttpError(
            fake_resp, b'{"error": "forbidden"}'
        )
        mock_get_service.return_value = service

        result = list_drive_folder_recursive("root")
        self.assertEqual(result, [])

    @patch('app.drive_folder.get_drive_service')
    def test_pagination_collects_all_pages(self, mock_get_service):
        service = MagicMock()
        page1 = MagicMock()
        page1.execute.return_value = {
            "files": [{"id": "f1", "name": "a.pdf", "mimeType": "application/pdf"}],
            "nextPageToken": "TOKEN2",
        }
        page2 = MagicMock()
        page2.execute.return_value = {
            "files": [{"id": "f2", "name": "b.pdf", "mimeType": "application/pdf"}],
        }
        service.files.return_value.list.side_effect = [page1, page2]
        mock_get_service.return_value = service

        result = list_drive_folder_recursive("root")
        names = {r["name"] for r in result}
        self.assertEqual(names, {"a.pdf", "b.pdf"})


if __name__ == '__main__':
    unittest.main()
