import asyncio
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.drive_mirror import (
    mirror_drive_image,
    mirror_project_drive_files,
    mirror_drive_folder,
    mirror_listed_drive_folder,
    mirror_external_image,
    mirror_project_external_images,
    get_or_create_folder_path,
)


def _service_with_lookup(existing_files=None, copy_result=None, copy_error=None, create_folder_id="new_folder"):
    """
    existing_files: dict query-substring -> [{"id":..,"name":..}] для files().list()
    Упрощённо: files().list() всегда возвращает existing_files.get(<после 'name = '>, [])
    по имени, которое мы ищем в query.
    """
    service = MagicMock()

    def list_side_effect(q, **kwargs):
        exec_mock = MagicMock()
        name = q.split("name = '")[1].split("'")[0] if "name = '" in q else None
        found = (existing_files or {}).get(name, [])
        exec_mock.execute.return_value = {"files": found}
        return exec_mock

    service.files.return_value.list.side_effect = list_side_effect

    create_exec = MagicMock()
    create_exec.execute.return_value = {"id": create_folder_id}
    service.files.return_value.create.return_value = create_exec

    copy_exec = MagicMock()
    if copy_error:
        copy_exec.execute.side_effect = copy_error
    else:
        copy_exec.execute.return_value = copy_result or {"id": "copied_id"}
    service.files.return_value.copy.return_value = copy_exec

    return service


class TestMirrorDriveImage(unittest.TestCase):

    def test_non_image_is_skipped(self):
        service = _service_with_lookup()
        result = mirror_drive_image(
            service, {"id": "f1", "name": "deed.pdf", "mimeType": "application/pdf"}, "dest"
        )
        self.assertEqual(result["status"], "skipped")
        service.files.return_value.copy.assert_not_called()

    def test_new_image_is_copied(self):
        service = _service_with_lookup(copy_result={"id": "copied_id"})
        result = mirror_drive_image(
            service, {"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}, "dest"
        )
        self.assertEqual(result["status"], "copied")
        self.assertEqual(result["file_id"], "copied_id")
        service.files.return_value.copy.assert_called_once()
        kwargs = service.files.return_value.copy.call_args.kwargs
        self.assertEqual(kwargs["fileId"], "f1")
        self.assertEqual(kwargs["body"]["parents"], ["dest"])

    def test_existing_image_is_not_copied_again(self):
        """Тест 17: повторный прогон по тому же проекту не создаёт дублей."""
        service = _service_with_lookup(existing_files={"render.jpg": [{"id": "already_there"}]})
        result = mirror_drive_image(
            service, {"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}, "dest"
        )
        self.assertEqual(result["status"], "exists")
        self.assertEqual(result["file_id"], "already_there")
        service.files.return_value.copy.assert_not_called()

    def test_copy_protection_produces_error_not_silent_skip(self):
        """Тест 15: запрет на копирование -> явная запись, не молчаливый пропуск."""
        from googleapiclient.errors import HttpError
        fake_resp = MagicMock(status=403)
        service = _service_with_lookup(copy_error=HttpError(fake_resp, b'{"error": "cannotCopyFile"}'))
        result = mirror_drive_image(
            service, {"id": "f1", "name": "render.jpg", "mimeType": "image/jpeg"}, "dest"
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("reason", result)


class TestMirrorProjectDriveFiles(unittest.TestCase):

    @patch('app.drive_mirror.get_drive_service')
    def test_gaps_collect_only_errors(self, mock_get_service):
        from googleapiclient.errors import HttpError
        fake_resp = MagicMock(status=403)

        service = MagicMock()

        def list_side_effect(q, **kwargs):
            exec_mock = MagicMock()
            exec_mock.execute.return_value = {"files": []}
            return exec_mock

        service.files.return_value.list.side_effect = list_side_effect
        create_exec = MagicMock()
        create_exec.execute.return_value = {"id": "project_folder"}
        service.files.return_value.create.return_value = create_exec

        copy_exec = MagicMock()
        copy_exec.execute.side_effect = [
            {"id": "ok_id"},
            HttpError(fake_resp, b'{"error": "cannotCopyFile"}'),
        ]
        service.files.return_value.copy.return_value = copy_exec

        mock_get_service.return_value = service

        drive_files = [
            {"id": "f1", "name": "ok.jpg", "mimeType": "image/jpeg"},
            {"id": "f2", "name": "blocked.jpg", "mimeType": "image/jpeg"},
            {"id": "f3", "name": "deed.pdf", "mimeType": "application/pdf"},
        ]
        summary = mirror_project_drive_files("Four Palms Villas", drive_files)

        self.assertEqual(len(summary["results"]), 3)
        self.assertEqual(len(summary["gaps"]), 1)
        self.assertIn("blocked.jpg", summary["gaps"][0])

    @patch('app.drive_mirror.get_drive_service')
    def test_folder_path_uses_project_and_unit_type(self, mock_get_service):
        service = MagicMock()

        seen_queries = []

        def list_side_effect(q, **kwargs):
            seen_queries.append(q)
            exec_mock = MagicMock()
            exec_mock.execute.return_value = {"files": []}
            return exec_mock

        service.files.return_value.list.side_effect = list_side_effect
        create_calls = []

        def create_side_effect(body, **kwargs):
            create_calls.append(body["name"])
            exec_mock = MagicMock()
            exec_mock.execute.return_value = {"id": f"id_{body['name']}"}
            return exec_mock

        service.files.return_value.create.side_effect = create_side_effect
        mock_get_service.return_value = service

        dest = get_or_create_folder_path(service, ["Four Palms Villas", "Studio with pool"], root_id="root")
        self.assertEqual(create_calls, ["Four Palms Villas", "Studio with pool"])
        self.assertTrue(any("Four Palms Villas" in q for q in seen_queries))


class TestIdentityDocumentsAreNeverMirrored(unittest.TestCase):
    """
    Реальный случай: в Legal-папке Four Palms лежат JPEG с KITAP/паспортами
    директоров. Это изображения, то есть фильтр по mimeType их пропускает -
    нужен отдельный запрет по имени, иначе паспорт уедет в постоянное зеркало.
    """

    def test_passport_image_is_skipped(self):
        service = _service_with_lookup()
        result = mirror_drive_image(
            service,
            {"id": "f1", "name": "PT Seacrest RE - KITAP Passport Vasily Pronin.jpg",
             "mimeType": "image/jpeg"},
            "dest",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "identity document")
        service.files.return_value.copy.assert_not_called()

    def test_ktp_image_is_skipped(self):
        service = _service_with_lookup()
        result = mirror_drive_image(
            service, {"id": "f1", "name": "KTP Direktur.jpg", "mimeType": "image/jpeg"}, "dest"
        )
        self.assertEqual(result["status"], "skipped")
        service.files.return_value.copy.assert_not_called()

    def test_identity_document_detected_by_parent_folder(self):
        """Файл может называться 'scan_01.jpg', а лежать в папке Passports."""
        service = _service_with_lookup()
        result = mirror_drive_image(
            service,
            {"id": "f1", "name": "scan_01.jpg", "mimeType": "image/jpeg",
             "path": "Legal/Passports"},
            "dest",
        )
        self.assertEqual(result["status"], "skipped")
        service.files.return_value.copy.assert_not_called()

    def test_ordinary_render_is_not_mistaken_for_document(self):
        """Обратная сторона: обычный рендер не должен ложно отсекаться."""
        service = _service_with_lookup()
        result = mirror_drive_image(
            service,
            {"id": "f1", "name": "3.1.jpg", "mimeType": "image/jpeg",
             "path": "Four Palms - Villa 1 - Renders/Interior"},
            "dest",
        )
        self.assertEqual(result["status"], "copied")


class TestSourceStructureIsPreserved(unittest.TestCase):
    """
    Рендеры одного проекта разложены по виллам и типам съёмки. Если сплющить их
    в одну папку, восстановить принадлежность картинки к вилле уже невозможно.
    """

    @patch('app.drive_mirror.get_drive_service')
    def test_nested_paths_create_matching_folders(self, mock_get_service):
        service = MagicMock()
        service.files.return_value.list.side_effect = (
            lambda q, **kw: MagicMock(**{"execute.return_value": {"files": []}})
        )

        created_paths = []

        def create_side_effect(body, **kwargs):
            created_paths.append((body["name"], body.get("parents", [None])[0]))
            return MagicMock(**{"execute.return_value": {"id": f"id_{body['name']}"}})

        service.files.return_value.create.side_effect = create_side_effect
        service.files.return_value.copy.return_value = MagicMock(
            **{"execute.return_value": {"id": "copied"}}
        )
        mock_get_service.return_value = service

        drive_files = [
            {"id": "a", "name": "1.jpg", "mimeType": "image/jpeg",
             "path": "Four Palms - Villa 1 - Renders/Interior"},
            {"id": "b", "name": "2.jpg", "mimeType": "image/jpeg",
             "path": "Four Palms - Villa 1 - Renders/Interior"},
            {"id": "c", "name": "3.jpg", "mimeType": "image/jpeg",
             "path": "Exterior/Latest Renders"},
        ]
        summary = mirror_project_drive_files("Four Palms Villas", drive_files)

        created_names = [n for n, _ in created_paths]
        self.assertIn("Interior", created_names)
        self.assertIn("Latest Renders", created_names)
        # Папка Interior создаётся один раз на два файла, а не на каждый файл
        self.assertEqual(created_names.count("Interior"), 1)
        self.assertTrue(all(r["status"] == "copied" for r in summary["results"]))

    @patch('app.drive_mirror.get_drive_service')
    def test_skipped_files_do_not_create_empty_folders(self, mock_get_service):
        service = MagicMock()
        service.files.return_value.list.side_effect = (
            lambda q, **kw: MagicMock(**{"execute.return_value": {"files": []}})
        )
        created_names = []

        def create_side_effect(body, **kwargs):
            created_names.append(body["name"])
            return MagicMock(**{"execute.return_value": {"id": f"id_{body['name']}"}})

        service.files.return_value.create.side_effect = create_side_effect
        mock_get_service.return_value = service

        drive_files = [
            {"id": "a", "name": "deed.pdf", "mimeType": "application/pdf", "path": "Legal"},
            {"id": "b", "name": "KTP.jpg", "mimeType": "image/jpeg", "path": "Legal"},
        ]
        mirror_project_drive_files("Four Palms Villas", drive_files)

        self.assertNotIn("Legal", created_names)


class TestMirrorDriveFolderKeepsItsOwnName(unittest.TestCase):
    """
    Регрессия на реальную ошибку прогона: у проекта в Notion шесть разных папок
    Drive, а path из листинга считается относительно переданной папки, поэтому
    её собственное имя терялось и файлы верхнего уровня ложились в корень
    проекта вперемешку с файлами из подпапок.
    """

    @patch('app.drive_mirror.get_drive_service')
    @patch('app.drive_folder.list_drive_folder_recursive')
    def test_source_folder_name_becomes_top_level(self, mock_list, mock_get_service):
        service = MagicMock()
        service.files.return_value.get.return_value = MagicMock(
            **{"execute.return_value": {"name": "Renders"}}
        )
        service.files.return_value.list.side_effect = (
            lambda q, **kw: MagicMock(**{"execute.return_value": {"files": []}})
        )
        created = []

        def create_side_effect(body, **kwargs):
            created.append(body["name"])
            return MagicMock(**{"execute.return_value": {"id": f"id_{body['name']}"}})

        service.files.return_value.create.side_effect = create_side_effect
        service.files.return_value.copy.return_value = MagicMock(
            **{"execute.return_value": {"id": "copied"}}
        )
        mock_get_service.return_value = service

        mock_list.return_value = [
            {"id": "i0", "name": "cover.jpg", "mimeType": "image/jpeg", "path": ""},
            {"id": "i1", "name": "still.jpg", "mimeType": "image/jpeg", "path": "Stills"},
        ]

        summary = mirror_drive_folder("src_folder", "Four Palms Villas")

        self.assertEqual(summary["source_folder_name"], "Renders")
        self.assertIn("Renders", created)
        self.assertIn("Stills", created)

    @patch('app.drive_mirror.get_drive_service')
    @patch('app.drive_folder.list_drive_folder_recursive')
    def test_listed_variant_does_not_call_listing_again(self, mock_list, mock_get_service):
        """
        process_generic_link уже вызвал list_drive_folder_recursive для Э1 -
        mirror_listed_drive_folder не должен листить ту же папку второй раз
        (лишний вызов Drive API на каждую ссылку).
        """
        service = MagicMock()
        service.files.return_value.get.return_value = MagicMock(
            **{"execute.return_value": {"name": "Renders"}}
        )
        service.files.return_value.list.return_value = MagicMock(
            **{"execute.return_value": {"files": []}}
        )
        service.files.return_value.create.side_effect = (
            lambda body, **kw: MagicMock(**{"execute.return_value": {"id": f"id_{body['name']}"}})
        )
        service.files.return_value.copy.return_value = MagicMock(
            **{"execute.return_value": {"id": "copied"}}
        )
        mock_get_service.return_value = service

        already_listed = [{"id": "i1", "name": "1.jpg", "mimeType": "image/jpeg", "path": ""}]
        summary = mirror_listed_drive_folder("src_folder", already_listed, "Four Palms Villas")

        mock_list.assert_not_called()
        self.assertEqual(summary["source_folder_name"], "Renders")
        self.assertEqual(summary["results"][0]["status"], "copied")


class TestMirrorExternalImage(unittest.TestCase):
    """Тест 18: изображение не с Drive (например, из Notion) -> скачать и загрузить."""

    def test_non_drive_image_is_downloaded_and_uploaded(self):
        async def run_test():
            fake_response = MagicMock()
            fake_response.status_code = 200
            fake_response.headers = {"content-type": "image/png"}
            fake_response.content = b"\x89PNG fake bytes"

            fake_client = MagicMock()
            fake_client.get = AsyncMock(return_value=fake_response)
            fake_client.__aenter__ = AsyncMock(return_value=fake_client)
            fake_client.__aexit__ = AsyncMock(return_value=False)

            service = MagicMock()
            service.files.return_value.list.return_value.execute.return_value = {"files": []}
            create_exec = MagicMock()
            create_exec.execute.return_value = {"id": "uploaded_id"}
            service.files.return_value.create.return_value = create_exec

            with patch('app.drive_mirror.get_drive_service', return_value=service), \
                 patch('httpx.AsyncClient', return_value=fake_client):
                result = await mirror_external_image(
                    "https://notion.so/image-uploads/plan.png", "dest_folder"
                )

            self.assertEqual(result["status"], "uploaded")
            self.assertEqual(result["file_id"], "uploaded_id")

        asyncio.run(run_test())

    def test_non_image_content_type_is_skipped(self):
        async def run_test():
            fake_response = MagicMock()
            fake_response.status_code = 200
            fake_response.headers = {"content-type": "text/html"}
            fake_response.content = b"<html></html>"

            fake_client = MagicMock()
            fake_client.get = AsyncMock(return_value=fake_response)
            fake_client.__aenter__ = AsyncMock(return_value=fake_client)
            fake_client.__aexit__ = AsyncMock(return_value=False)

            service = MagicMock()
            service.files.return_value.list.return_value.execute.return_value = {"files": []}

            with patch('app.drive_mirror.get_drive_service', return_value=service), \
                 patch('httpx.AsyncClient', return_value=fake_client):
                result = await mirror_external_image(
                    "https://notion.so/not-an-image", "dest_folder"
                )

            self.assertEqual(result["status"], "skipped")
            service.files.return_value.create.assert_not_called()

        asyncio.run(run_test())

    def test_existing_external_image_is_not_downloaded_again(self):
        async def run_test():
            service = MagicMock()
            service.files.return_value.list.return_value.execute.return_value = {
                "files": [{"id": "already_uploaded", "name": "plan.png"}]
            }

            with patch('app.drive_mirror.get_drive_service', return_value=service), \
                 patch('httpx.AsyncClient') as mock_client_cls:
                result = await mirror_external_image(
                    "https://notion.so/image-uploads/plan.png", "dest_folder"
                )

            self.assertEqual(result["status"], "exists")
            mock_client_cls.assert_not_called()

        asyncio.run(run_test())


class TestExtractMirrorAirtableFields(unittest.TestCase):

    def test_extracts_folder_url_and_cover_image(self):
        from app.drive_mirror import extract_mirror_airtable_fields

        summary = {
            "dest_folder_id": "folder_123",
            "results": [
                {"name": "doc.pdf", "status": "skipped"},
                {"name": "render1.jpg", "status": "copied", "file_id": "img_456"},
                {"name": "render2.jpg", "status": "copied", "file_id": "img_789"},
            ]
        }
        fields = extract_mirror_airtable_fields(summary)
        # Зеркало едет в Renders: 'Link to Developer’s Kit' - ссылка застройщика,
        # подменять первоисточник своей копией нельзя.
        self.assertEqual(
            fields.get("Renders"),
            "https://drive.google.com/drive/folders/folder_123",
        )
        self.assertNotIn("Link to Developer’s Kit (Rus)", fields)
        # sz=w2000 - канон формата картинки из RULES.md.
        self.assertEqual(fields.get("Img"), [{"url": "https://drive.google.com/thumbnail?id=img_456&sz=w2000"}])

    def test_cover_skips_document_scans(self):
        """Обложкой проекта не должен становиться скан документа из Legal-папки."""
        from app.drive_mirror import extract_mirror_airtable_fields

        summary = {
            "dest_folder_id": "folder_123",
            "results": [
                {"name": "SLF reg.jpeg", "status": "copied", "file_id": "scan_1"},
                {"name": "3.1.jpg", "status": "copied", "file_id": "render_1", "path": "Renders"},
            ]
        }
        fields = extract_mirror_airtable_fields(summary)
        self.assertEqual(
            fields.get("Img"),
            [{"url": "https://drive.google.com/thumbnail?id=render_1&sz=w2000"}],
        )

    def test_handles_empty_summary(self):
        from app.drive_mirror import extract_mirror_airtable_fields

        fields = extract_mirror_airtable_fields({})
        self.assertEqual(fields, {})


if __name__ == '__main__':
    unittest.main()
