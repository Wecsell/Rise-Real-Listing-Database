"""
Зеркалирование изображений проекта на личный Google Drive владельца
(implementation_plan.md, Э6). Разовый снимок на момент парсинга, не
синхронизация - повторный прогон по тому же проекту не должен плодить дубли.

Область: только изображения (рендеры, планировки). Видео сознательно не
зеркалируются (решение владельца 2026-08-01) - юридические документы тоже не
копируем, они нужны один раз при разборе, дальше живут как факты в базе.

Документы личности запрещены отдельным правилом, а не "по совпадению": в
Legal-папке Four Palms лежат JPEG с KITAP/паспортами директоров, то есть
фильтра по типу файла тут недостаточно - паспорт это тоже картинка.

Источники бывают двух видов:
- уже на Drive (из list_drive_folder_recursive) - копируются files.copy,
  на стороне Google, байты через нашу машину не идут;
- не на Drive (например, картинки, встроенные в саму страницу Notion) -
  для них единственный путь - скачать и загрузить, files.copy тут неприменим.
"""
import asyncio
import io
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from app.drive_auth import get_drive_service

logger = logging.getLogger("DriveMirror")

ROOT_FOLDER_ID = os.environ.get("GDRIVE_MIRROR_ROOT_ID", "1ec1sDa_CmBtjGSmW4VIAyqdFHjfye8Jn")

FOLDER_MIME = "application/vnd.google-apps.folder"
IMAGE_MIME_PREFIX = "image/"

_DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Документы, удостоверяющие личность. План (Э1, skip-лист) требует не скачивать
# и не парсить их; для зеркала это отдельный, более жёсткий запрет - копия живёт
# на нашем диске бессрочно, в отличие от временного файла при разборе.
#
# Не теоретическая предосторожность: в Legal-папке Four Palms реально лежат три
# JPEG с KITAP/паспортами директоров (Vasily Pronin, Oleg Kuklin, Pavel Lukoianov).
# До этой проверки единственной защитой было "мы просто не передадим сюда Legal" -
# то есть совпадение, а не правило. План прямо требует зафиксировать запрет явно.
_SKIP_NAME_RE = re.compile(
    r"passport|pasport|\bktp\b|kitap|\bid[\s_-]?card\b|паспорт|удостоверен",
    re.IGNORECASE,
)


def _is_image(mime_type: Optional[str]) -> bool:
    return bool(mime_type) and mime_type.startswith(IMAGE_MIME_PREFIX)


def is_identity_document(name: Optional[str], path: str = "") -> bool:
    """Имя файла (или папки на его пути) выглядит как документ личности."""
    haystack = f"{path or ''}/{name or ''}"
    return bool(_SKIP_NAME_RE.search(haystack))


def _escape_name(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _find_child_folder(service, parent_id: str, name: str) -> Optional[str]:
    query = (
        f"'{parent_id}' in parents and name = '{_escape_name(name)}' "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    response = service.files().list(
        q=query, fields="files(id, name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files", [])
    return files[0]["id"] if files else None


def _create_child_folder(service, parent_id: str, name: str) -> str:
    metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    created = service.files().create(
        body=metadata, fields="id", supportsAllDrives=True
    ).execute()
    return created["id"]


def get_or_create_folder_path(
    service, path_parts: List[str], root_id: str = ROOT_FOLDER_ID
) -> str:
    """
    Находит/создаёт путь /{Project Name}/{Unit Type}/ под корнем зеркала.
    Идемпотентно по конструкции: ищет существующую подпапку по имени прежде
    чем создавать, повторный вызов с тем же путём не плодит дубли папок.
    """
    current = root_id
    for part in path_parts:
        if not part:
            continue
        existing = _find_child_folder(service, current, part)
        current = existing or _create_child_folder(service, current, part)
    return current


def _find_existing_file(service, parent_id: str, name: str) -> Optional[str]:
    query = f"'{parent_id}' in parents and name = '{_escape_name(name)}' and trashed = false"
    response = service.files().list(
        q=query, fields="files(id, name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files", [])
    return files[0]["id"] if files else None


def mirror_drive_image(
    service, file_info: Dict[str, Any], dest_folder_id: str
) -> Dict[str, Any]:
    """
    Копирует один файл, уже лежащий на Drive, через files.copy (копирование
    на стороне Google - дешевле и без трафика через нашу машину). Ярлыки уже
    разыменованы выше по цепочке, в list_drive_folder_recursive - сюда всегда
    приходит id реального файла, не указателя.

    Возвращает {"status": ..., "name": ..., ...}:
    - "skipped" - не изображение, не наша область (юр. документы, паспорта);
    - "exists" - файл с этим именем уже в целевой папке, повторно не копируем;
    - "copied" - успех;
    - "error" - Drive отказал (например, запрет на копирование застройщиком) -
      это должно попасть в Gaps явной записью, не потеряться молча.
    """
    name = file_info.get("name") or file_info.get("id")
    if not _is_image(file_info.get("mimeType")):
        return {"status": "skipped", "name": name, "reason": "not an image"}

    if is_identity_document(name, file_info.get("path", "")):
        logger.info(f"🔒 Пропущен документ личности, не копируем: {name}")
        return {"status": "skipped", "name": name, "reason": "identity document"}

    existing = _find_existing_file(service, dest_folder_id, name)
    if existing:
        return {"status": "exists", "name": name, "file_id": existing}

    try:
        copied = service.files().copy(
            fileId=file_info["id"],
            body={"name": name, "parents": [dest_folder_id]},
            supportsAllDrives=True,
        ).execute()
        return {"status": "copied", "name": name, "file_id": copied["id"]}
    except HttpError as e:
        logger.warning(f"⚠️ Копирование не удалось для {name}: {e}")
        return {"status": "error", "name": name, "reason": str(e)}


async def mirror_external_image(
    url: str, dest_folder_id: str, name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Для изображений не с Drive (например, встроенных прямо в страницу
    Notion): files.copy тут неприменим, единственный путь - скачать и
    загрузить. Идемпотентность та же, что у mirror_drive_image - проверка
    по имени в целевой папке перед загрузкой.
    """
    service = get_drive_service()
    filename = name or (url.rstrip("/").rsplit("/", 1)[-1] or "image")

    existing = await asyncio.to_thread(_find_existing_file, service, dest_folder_id, filename)
    if existing:
        return {"status": "exists", "name": filename, "file_id": existing}

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0, headers=_DOWNLOAD_HEADERS
        ) as client:
            res = await client.get(url)
    except Exception as e:
        return {"status": "error", "name": filename, "reason": str(e)}

    if res.status_code != 200:
        return {"status": "error", "name": filename, "reason": f"HTTP {res.status_code}"}

    content_type = res.headers.get("content-type", "").split(";")[0].strip()
    if not _is_image(content_type):
        return {"status": "skipped", "name": filename, "reason": "not an image"}

    def _upload():
        media = MediaIoBaseUpload(io.BytesIO(res.content), mimetype=content_type, resumable=False)
        return service.files().create(
            body={"name": filename, "parents": [dest_folder_id]},
            media_body=media, fields="id", supportsAllDrives=True,
        ).execute()

    try:
        uploaded = await asyncio.to_thread(_upload)
        return {"status": "uploaded", "name": filename, "file_id": uploaded["id"]}
    except HttpError as e:
        logger.warning(f"⚠️ Загрузка внешнего изображения не удалась для {filename}: {e}")
        return {"status": "error", "name": filename, "reason": str(e)}


def mirror_project_drive_files(
    project_name: str,
    drive_files: List[Dict[str, Any]],
    unit_type: Optional[str] = None,
    root_id: str = ROOT_FOLDER_ID,
) -> Dict[str, Any]:
    """
    Точка входа Э6 для файлов, уже развёрнутых list_drive_folder_recursive.
    Складывает изображения в /{Project Name}/{Unit Type}/ (или просто
    /{Project Name}/, если разделения по типам юнитов в источнике нет).

    Возвращает сводку по каждому файлу плюс gaps - человекочитаемые записи
    об отказах, готовые к слиянию через app.gaps.merge_gaps.
    """
    service = get_drive_service()
    base_parts = [project_name] + ([unit_type] if unit_type else [])
    dest_folder_id = get_or_create_folder_path(service, base_parts, root_id)

    # Структура источника повторяется, а не сплющивается: у одного проекта
    # рендеры разложены по виллам и по типам съёмки (Villa 1/Interior,
    # Exterior/Latest Renders...), и 200 файлов в одной папке без разделения
    # неотличимы друг от друга - какая картинка к какой вилле, восстановить
    # уже нельзя. path приходит из list_drive_folder_recursive.
    folder_cache: Dict[str, str] = {"": dest_folder_id}

    def _dest_for(rel_path: str) -> str:
        rel_path = (rel_path or "").strip("/")
        if rel_path in folder_cache:
            return folder_cache[rel_path]
        parts = [p for p in rel_path.split("/") if p]
        folder_id = get_or_create_folder_path(service, base_parts + parts, root_id)
        folder_cache[rel_path] = folder_id
        return folder_id

    results = []
    for f in (drive_files or []):
        # Папку под файл создаём только когда файл реально подлежит копированию,
        # иначе на диске появятся пустые папки от пропущенных PDF/видео/паспортов.
        if not _is_image(f.get("mimeType")) or is_identity_document(f.get("name"), f.get("path", "")):
            results.append(mirror_drive_image(service, f, dest_folder_id))
            continue
        results.append(mirror_drive_image(service, f, _dest_for(f.get("path", ""))))

    gaps = [
        f"Drive mirror failed for {r['name']}: {r['reason']}"
        for r in results if r["status"] == "error"
    ]
    return {"dest_folder_id": dest_folder_id, "results": results, "gaps": gaps}


def get_drive_folder_name(service, folder_id: str) -> Optional[str]:
    try:
        meta = service.files().get(
            fileId=folder_id, fields="name", supportsAllDrives=True
        ).execute()
        return meta.get("name")
    except HttpError as e:
        logger.warning(f"⚠️ Не удалось прочитать имя папки {folder_id}: {e}")
        return None


def _prefix_with_source_folder_name(
    service, folder_id: str, files: List[Dict[str, Any]]
) -> Optional[str]:
    """
    Дозаполняет path именем самой папки-источника.

    path из list_drive_folder_recursive считается ОТНОСИТЕЛЬНО переданной
    папки, поэтому файлы её верхнего уровня приходят с пустым path и ложатся
    прямо в корень проекта. Для проекта, у которого в Notion висит шесть
    разных папок (рендеры, видео, брошюры...), это сваливает их содержимое
    в одну кучу - проверено на Four Palms.
    """
    folder_name = get_drive_folder_name(service, folder_id)
    if folder_name:
        for f in files:
            prefix = f.get("path") or ""
            f["path"] = f"{folder_name}/{prefix}" if prefix else folder_name
    return folder_name


def mirror_drive_folder(
    folder_id: str,
    project_name: str,
    max_depth: int = 5,
    root_id: str = ROOT_FOLDER_ID,
) -> Dict[str, Any]:
    """
    Зеркалирует папку Drive целиком, сохраняя её собственное имя верхним уровнем:
    /{Project Name}/{имя исходной папки}/{вложенные папки}/файл.

    Делает свой собственный листинг - для случая, когда список файлов ещё не
    получен вызывающим кодом. Если листинг уже сделан (как в process_generic_link,
    который и так обходит Drive-папку для Э1), используй mirror_listed_drive_folder
    - не дублировать вызовы Drive API.
    """
    from app.drive_folder import list_drive_folder_recursive

    service = get_drive_service()
    files = list_drive_folder_recursive(folder_id, max_depth=max_depth)
    folder_name = _prefix_with_source_folder_name(service, folder_id, files)

    summary = mirror_project_drive_files(project_name, files, root_id=root_id)
    summary["source_folder_name"] = folder_name
    return summary


def mirror_listed_drive_folder(
    folder_id: str,
    files: List[Dict[str, Any]],
    project_name: str,
    root_id: str = ROOT_FOLDER_ID,
) -> Dict[str, Any]:
    """
    Как mirror_drive_folder, но для уже полученного list_drive_folder_recursive()
    списка файлов - не делает повторный листинг той же папки.
    """
    service = get_drive_service()
    folder_name = _prefix_with_source_folder_name(service, folder_id, files)

    summary = mirror_project_drive_files(project_name, files, root_id=root_id)
    summary["source_folder_name"] = folder_name
    return summary


async def mirror_project_external_images(
    project_name: str,
    image_urls: List[str],
    unit_type: Optional[str] = None,
    root_id: str = ROOT_FOLDER_ID,
) -> Dict[str, Any]:
    """
    Точка входа Э6 для изображений не с Drive (найдены как ссылки, например
    внутри содержимого Notion). Та же целевая папка, что и у Drive-файлов
    того же проекта - зеркало не различает происхождение на выходе.
    """
    service = get_drive_service()
    path_parts = [project_name] + ([unit_type] if unit_type else [])
    dest_folder_id = await asyncio.to_thread(
        get_or_create_folder_path, service, path_parts, root_id
    )

    results = []
    for url in image_urls or []:
        results.append(await mirror_external_image(url, dest_folder_id))

    gaps = [
        f"Drive mirror failed for {r['name']}: {r['reason']}"
        for r in results if r["status"] == "error"
    ]
    return {"dest_folder_id": dest_folder_id, "results": results, "gaps": gaps}


def extract_mirror_airtable_fields(mirror_summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Извлекает поля Airtable из результата зеркалирования:
    - Renders: ссылка на папку-зеркало на нашем Drive;
    - Img: обложка проекта (первый скопированный рендер).

    Именно Renders, а не 'Link to Developer’s Kit (Rus)': последнее - ссылка на
    материалы САМОГО застройщика, первоисточник. Записав туда своё зеркало, мы
    теряем оригинал и на следующем прогоне выкачиваем собственную копию вместо
    материалов застройщика. Поле Projects.Renders заведено под это 02.08.2026
    (в Units одноимённое поле было изначально).
    """
    from app.doc_router import is_document_scan

    fields: Dict[str, Any] = {}
    if not mirror_summary or not isinstance(mirror_summary, dict):
        return fields

    dest_folder_id = mirror_summary.get("dest_folder_id")
    if dest_folder_id:
        fields["Renders"] = f"https://drive.google.com/drive/folders/{dest_folder_id}"

    results = mirror_summary.get("results") or []
    for item in results:
        status = item.get("status")
        # mirror_drive_image returns 'file_id', not 'id'
        file_id = item.get("file_id") or item.get("id")
        if status not in ("copied", "exists", "uploaded") or not file_id:
            continue
        # Обложкой не может стать скан документа: в зеркало попадает и "SLF reg.jpeg".
        if is_document_scan({
            "mimeType": "image/jpeg",
            "name": item.get("name"),
            "path": item.get("path", ""),
        }):
            continue
        # Формат URL картинки закреплён каноном базы (RULES.md): sz=w2000.
        fields["Img"] = [{"url": f"https://drive.google.com/thumbnail?id={file_id}&sz=w2000"}]
        break

    return fields
