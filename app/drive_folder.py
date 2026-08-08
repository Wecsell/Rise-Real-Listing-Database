"""
Листинг содержимого папок Google Drive (implementation_plan.md, Э1: "Google
Drive — папка" была заглушкой - process_generic_link просто помечал ссылку
и выходил, ни один файл внутри не читался).

Читать может любой авторизованный аккаунт, включая доступ по ссылке "anyone
with the link" - для чтения (в отличие от записи) OAuth-токен владельца из
app/drive_auth.py подходит без дополнительной настройки.
"""
import logging
import os
from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError

from app.drive_auth import get_drive_service

logger = logging.getLogger("DriveFolder")

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Предел вложенности обхода. Не константа в коде, потому что зависит от того,
# как застройщик разложил свои материалы, а не от нашей логики: у Y-WAY папка
# рендеров уходит на шесть уровней, и лимит 5 обрезал листинг молча.
# Верхняя граница удерживает обход от разрастания на битой структуре.
_MAX_DEPTH_CEILING = 12


def _bounded_depth_env(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r не число, беру %s", name, raw, default)
        return default
    if value < 1:
        logger.warning("%s=%s меньше 1, беру %s", name, value, default)
        return default
    if value > maximum:
        logger.warning("%s=%s больше предела %s, беру предел", name, value, maximum)
        return maximum
    return value


DEFAULT_MAX_DEPTH = _bounded_depth_env("GDRIVE_MAX_DEPTH", 8, _MAX_DEPTH_CEILING)

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


def download_drive_file(file_id: str, dest_path: str) -> bool:
    """
    Скачивает файл с Drive по id через OAuth-клиент владельца.

    Не через публичный URL (download_file_from_url в link_fetcher): у файла в
    папке застройщика доступ бывает «по ссылке для тех, у кого есть ссылка»,
    и анонимный GET на него отдаёт HTML логина, а не документ. Авторизованный
    вызов API работает там же, где сработал листинг.

    Вызывающий обязан удалить файл сразу после извлечения (правило проекта:
    временные файлы не задерживаются на диске).
    """
    from googleapiclient.http import MediaIoBaseDownload

    service = get_drive_service()
    try:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except HttpError as e:
        logger.error(f"Не удалось скачать файл Drive {file_id}: {e}")
        return False


def list_drive_folder_recursive(
    folder_id: str, max_depth: Optional[int] = None, _depth: int = 0,
    _visited: Optional[set] = None, _path: str = "",
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
    должна ссылаться сама на себя через цепочку ярлыков. Значение по умолчанию
    берётся из GDRIVE_MAX_DEPTH: у застройщиков встречаются папки глубже пяти
    уровней (кит Y-WAY - шесть), и прежний жёсткий лимит 5 молча обрезал
    листинг, оставляя часть рендеров незеркалированными.
    """
    if max_depth is None:
        max_depth = DEFAULT_MAX_DEPTH
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
