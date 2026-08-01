"""
Листинг содержимого папок Google Drive (implementation_plan.md, Э1: "Google
Drive — папка" была заглушкой - process_generic_link просто помечал ссылку
и выходил, ни один файл внутри не читался).

Читать может любой авторизованный аккаунт, включая доступ по ссылке "anyone
with the link" - для чтения (в отличие от записи) OAuth-токен владельца из
app/drive_auth.py подходит без дополнительной настройки.
"""
import logging
from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError

from app.drive_auth import get_drive_service

logger = logging.getLogger("DriveFolder")

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FOLDER_MIME = "application/vnd.google-apps.folder"

_FIELDS = "nextPageToken, files(id, name, mimeType, size, shortcutDetails)"


def _list_children(service, folder_id: str) -> List[Dict[str, Any]]:
    """Один уровень: все файлы/папки, у которых folder_id - прямой родитель."""
    files: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        response = service.files().list(
            q=query, fields=_FIELDS, pageToken=page_token, pageSize=100,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def list_drive_folder_recursive(
    folder_id: str, max_depth: int = 5, _depth: int = 0, _visited: Optional[set] = None,
    _path: str = "",
) -> List[Dict[str, Any]]:
    """
    Разворачивает папку рекурсивно, разыменовывая ярлыки ("Shortcut to Shared
    folder" - реальный случай из Legal-папки Four Palms: ярлык копирует
    указатель, а не содержимое, без разыменования листинг вернул бы пустышку).

    Возвращает плоский список файлов (без подпапок как отдельных записей) с
    полями: id, name, mimeType, size (может отсутствовать у папок/Google Docs),
    path - путь папок относительно переданной folder_id ("" для файлов в корне,
    "Villa 1/Interior" для вложенных). Без него зеркало (Э6) не может повторить
    структуру источника и сваливает все картинки в одну кучу.

    Кап на глубину и visited-set по id папки - защита от циклов, папка не
    должна ссылаться сама на себя через цепочку ярлыков.
    """
    if _visited is None:
        _visited = set()
    if folder_id in _visited or _depth > max_depth:
        if _depth > max_depth:
            logger.warning(f"⚠️ Превышен лимит глубины ({_depth}/{max_depth}) для папки {folder_id}")
        return []
    _visited.add(folder_id)

    service = get_drive_service()
    try:
        children = _list_children(service, folder_id)
    except HttpError as e:
        logger.error(f"Ошибка листинга папки Drive {folder_id}: {e}")
        return []

    results: List[Dict[str, Any]] = []
    for item in children:
        mime = item.get("mimeType")

        if mime == SHORTCUT_MIME:
            target = (item.get("shortcutDetails") or {}).get("targetId")
            target_mime = (item.get("shortcutDetails") or {}).get("targetMimeType")
            if not target:
                continue
            if target_mime == FOLDER_MIME:
                sub_path = f"{_path}/{item.get('name')}" if _path else str(item.get("name"))
                results.extend(
                    list_drive_folder_recursive(
                        target, max_depth, _depth + 1, _visited, sub_path
                    )
                )
            else:
                results.append({
                    "id": target,
                    "name": item.get("name"),
                    "mimeType": target_mime,
                    "size": item.get("size"),
                    "path": _path,
                })
            continue

        if mime == FOLDER_MIME:
            sub_path = f"{_path}/{item.get('name')}" if _path else str(item.get("name"))
            results.extend(
                list_drive_folder_recursive(
                    item["id"], max_depth, _depth + 1, _visited, sub_path
                )
            )
            continue

        results.append({
            "id": item["id"],
            "name": item.get("name"),
            "mimeType": mime,
            "size": item.get("size"),
            "path": _path,
        })

    return results
