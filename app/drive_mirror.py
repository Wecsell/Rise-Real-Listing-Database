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
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from app.drive_auth import get_drive_service
from app.url_safety import (
    UnsafeUrlError,
    ANY_PUBLIC_HOST,
    redact_url,
    stream_response_to_tempfile,
    stream_safe_url,
    validate_url_origin,
)

logger = logging.getLogger("DriveMirror")

# Mirroring is a write operation.  It must never silently fall back to a
# production folder when a deployment is misconfigured.
ROOT_FOLDER_ID = os.environ.get("GDRIVE_MIRROR_ROOT_ID", "").strip() or None

FOLDER_MIME = "application/vnd.google-apps.folder"
IMAGE_MIME_PREFIX = "image/"

_DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_ALLOWED_EXTERNAL_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/gif",
})


def _bounded_int_env(name: str, default: int, maximum: int) -> int:
    """Return a positive bounded integer without making a bad env fatal."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default
    return max(1, min(value, maximum))


MAX_EXTERNAL_IMAGE_BYTES = _bounded_int_env(
    "MAX_EXTERNAL_IMAGE_BYTES", 10 * 1024 * 1024, 25 * 1024 * 1024
)
MAX_EXTERNAL_IMAGES_PER_PROJECT = _bounded_int_env(
    "MAX_EXTERNAL_IMAGES_PER_PROJECT", 10, 25
)


def require_mirror_root_id(root_id: Optional[str] = None) -> str:
    """Return the configured mirror root or stop before any Drive write."""
    resolved = str(root_id or ROOT_FOLDER_ID or "").strip()
    if not resolved:
        raise RuntimeError(
            "GDRIVE_MIRROR_ROOT_ID must be explicitly configured before mirroring files"
        )
    return resolved

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
    service, path_parts: List[str], root_id: Optional[str] = None
) -> str:
    """
    Находит/создаёт путь /{Project Name}/{Unit Type}/ под корнем зеркала.
    Идемпотентно по конструкции: ищет существующую подпапку по имени прежде
    чем создавать, повторный вызов с тем же путём не плодит дубли папок.
    """
    current = require_mirror_root_id(root_id)
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
    try:
        validate_url_origin(url, allowed_hosts=ANY_PUBLIC_HOST)
    except UnsafeUrlError as exc:
        logger.warning("Rejected unsafe external image URL %s: %s", redact_url(url), exc)
        return {
            "status": "skipped",
            "name": name or "image",
            "reason": str(exc),
            # Отличаем «ссылку отвергли» от безобидных пропусков (видео,
            # паспорт): это НЕ норма, а несобранные рендеры, и об этом обязан
            # узнать Gaps. Хост тут уже не ограничен (ANY_PUBLIC_HOST), так что
            # отказ означает реальную небезопасность: не HTTPS, логин-пароль в
            # URL, нестандартный порт или приватный IP в ответе DNS.
            "unsafe_url": True,
        }

    url_path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    filename = name or os.path.basename(url_path.rstrip("/")) or "image"
    # The name becomes a Drive filename.  Keep it predictable and prevent URL
    # paths such as ../../foo or control characters from leaking into Drive.
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(". ") or "image"
    filename = filename[:180]

    service = get_drive_service()

    existing = await asyncio.to_thread(_find_existing_file, service, dest_folder_id, filename)
    if existing:
        return {"status": "exists", "name": filename, "file_id": existing}

    temp_path: Optional[str] = None
    try:
        async with stream_safe_url(
            url,
            timeout=30.0,
            headers=_DOWNLOAD_HEADERS,
            allowed_hosts=ANY_PUBLIC_HOST,
        ) as response:
            if response.status_code != 200:
                return {
                    "status": "error",
                    "name": filename,
                    "reason": f"HTTP {response.status_code}",
                }

            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in _ALLOWED_EXTERNAL_IMAGE_TYPES:
                return {"status": "skipped", "name": filename, "reason": "not a supported image"}

            suffix = os.path.splitext(filename)[1].lower()
            if not suffix or len(suffix) > 10:
                suffix = ".img"
            temp_path = await stream_response_to_tempfile(
                response,
                suffix=suffix,
                max_bytes=MAX_EXTERNAL_IMAGE_BYTES,
            )

        def _upload():
            media = MediaFileUpload(temp_path, mimetype=content_type, resumable=False)
            return service.files().create(
                body={"name": filename, "parents": [dest_folder_id]},
                media_body=media, fields="id", supportsAllDrives=True,
            ).execute()

        uploaded = await asyncio.to_thread(_upload)
        return {"status": "uploaded", "name": filename, "file_id": uploaded["id"]}
    except UnsafeUrlError as exc:
        logger.warning("External image fetch rejected for %s: %s", redact_url(url), exc)
        return {"status": "skipped", "name": filename, "reason": str(exc)}
    except HttpError as e:
        logger.warning(f"⚠️ Загрузка внешнего изображения не удалась для {filename}: {e}")
        return {"status": "error", "name": filename, "reason": str(e)}
    except Exception as exc:
        logger.warning("External image fetch/upload failed for %s: %s", redact_url(url), exc)
        return {"status": "error", "name": filename, "reason": str(exc)}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as exc:
                logger.warning("Could not remove temporary image %s: %s", temp_path, exc)


def mirror_project_drive_files(
    project_name: str,
    drive_files: List[Dict[str, Any]],
    unit_type: Optional[str] = None,
    root_id: Optional[str] = None,
    subfolder: Optional[str] = None,
    developer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Точка входа Э6 для файлов, уже развёрнутых list_drive_folder_recursive.

    Складывает изображения в /{Developer}/{Project Name}/{Unit Type}/{subfolder}/
    (лишние уровни опускаются, если аргументы не переданы).

    Уровень застройщика - решение владельца (06.08.2026): зеркало повторяет
    структуру базы Developer -> Project -> Unit, поэтому найти папку можно от
    застройщика, не помня названий всех его проектов. Без него в корне зеркала
    лежали проекты вперемешку от всех застройщиков.

    subfolder нужен, когда у одного типа юнита в источнике НЕСКОЛЬКО папок
    рендеров: у K-Village это "1BR: 1-7 | 8-14 | 15-22" - три набора по номерам
    вилл. Без разделения они сливаются в одну папку, и файлы с одинаковыми
    именами из разных наборов отбрасываются как уже существующие: у K-Village
    так потерялось 6 рендеров (06.08.2026). Это тот же принцип, что и с path
    ниже - структура источника повторяется, а не сплющивается.

    Возвращает сводку по каждому файлу плюс gaps - человекочитаемые записи
    об отказах, готовые к слиянию через app.gaps.merge_gaps.
    """
    resolved_root_id = require_mirror_root_id(root_id)
    service = get_drive_service()
    base_parts = (
        ([developer] if developer else [])
        + [project_name]
        + ([unit_type] if unit_type else [])
        + ([subfolder] if subfolder else [])
    )
    dest_folder_id = get_or_create_folder_path(service, base_parts, resolved_root_id)

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
        folder_id = get_or_create_folder_path(service, base_parts + parts, resolved_root_id)
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
    max_depth: Optional[int] = None,
    root_id: Optional[str] = None,
    developer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Зеркалирует папку Drive целиком, сохраняя её собственное имя верхним уровнем:
    /{Developer}/{Project Name}/{имя исходной папки}/{вложенные папки}/файл.

    max_depth=None означает GDRIVE_MAX_DEPTH (см. drive_folder): жёсткая пятёрка
    здесь обрезала листинг у застройщиков с глубокой структурой.

    Делает свой собственный листинг - для случая, когда список файлов ещё не
    получен вызывающим кодом. Если листинг уже сделан (как в process_generic_link,
    который и так обходит Drive-папку для Э1), используй mirror_listed_drive_folder
    - не дублировать вызовы Drive API.
    """
    from app.drive_folder import list_drive_folder_recursive

    resolved_root_id = require_mirror_root_id(root_id)
    service = get_drive_service()
    files = list_drive_folder_recursive(folder_id, max_depth=max_depth)
    folder_name = _prefix_with_source_folder_name(service, folder_id, files)

    summary = mirror_project_drive_files(project_name, files, root_id=resolved_root_id,
                                        developer=developer)
    summary["source_folder_name"] = folder_name
    return summary


def mirror_listed_drive_folder(
    folder_id: str,
    files: List[Dict[str, Any]],
    project_name: str,
    root_id: Optional[str] = None,
    developer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Как mirror_drive_folder, но для уже полученного list_drive_folder_recursive()
    списка файлов - не делает повторный листинг той же папки.
    """
    resolved_root_id = require_mirror_root_id(root_id)
    service = get_drive_service()
    folder_name = _prefix_with_source_folder_name(service, folder_id, files)

    summary = mirror_project_drive_files(project_name, files, root_id=resolved_root_id,
                                        developer=developer)
    summary["source_folder_name"] = folder_name
    return summary


async def mirror_project_external_images(
    project_name: str,
    image_urls: List[str],
    unit_type: Optional[str] = None,
    root_id: Optional[str] = None,
    developer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Точка входа Э6 для изображений не с Drive (найдены как ссылки, например
    внутри содержимого Notion). Та же целевая папка, что и у Drive-файлов
    того же проекта - зеркало не различает происхождение на выходе.

    developer обязателен по тому же канону, что и в mirror_project_drive_files:
    зеркало повторяет структуру базы Developer -> Project -> Unit. Без него
    внешние картинки ложились в корень зеркала вперемешку от всех застройщиков,
    хотя Drive-файлы того же проекта уходили в /{Developer}/{Project}/ - один
    проект оказывался разорван на две папки (поймано 08.08.2026 на Garden
    Villa I, галерея BREIG).
    """
    resolved_root_id = require_mirror_root_id(root_id)
    service = get_drive_service()
    path_parts = (
        ([developer] if developer else [])
        + [project_name]
        + ([unit_type] if unit_type else [])
    )
    dest_folder_id = await asyncio.to_thread(
        get_or_create_folder_path, service, path_parts, resolved_root_id
    )

    unique_urls = list(dict.fromkeys(url for url in (image_urls or []) if isinstance(url, str)))
    results = []
    for url in unique_urls[:MAX_EXTERNAL_IMAGES_PER_PROJECT]:
        results.append(await mirror_external_image(url, dest_folder_id))

    if len(unique_urls) > MAX_EXTERNAL_IMAGES_PER_PROJECT:
        results.append({
            "status": "skipped",
            "name": "additional external images",
            "reason": f"limit {MAX_EXTERNAL_IMAGES_PER_PROJECT}",
        })

    gaps = [
        f"Drive mirror failed for {r['name']}: {r['reason']}"
        for r in results if r["status"] == "error"
    ]
    # Отвергнутая ссылка раньше терялась: она приходит как "skipped", а в gaps
    # попадали только "error". Целая галерея могла быть отвергнута, а прогон
    # рапортовал успех с пустым зеркалом (Garden Villa I, 10 из 10, 08.08.2026).
    unsafe = [r for r in results if r.get("unsafe_url")]
    if unsafe:
        reasons = sorted({r["reason"] for r in unsafe})
        gaps.append(
            f"External images not mirrored: {len(unsafe)} of {len(unique_urls)} "
            f"rejected as unsafe ({'; '.join(reasons)})"
        )
    if len(unique_urls) > MAX_EXTERNAL_IMAGES_PER_PROJECT:
        gaps.append(
            f"External image limit reached: processed {MAX_EXTERNAL_IMAGES_PER_PROJECT} of {len(unique_urls)}"
        )
    return {"dest_folder_id": dest_folder_id, "results": results, "gaps": gaps}


# Приоритет обложки (владелец, 06.08.2026): сначала общий план комплекса,
# иначе экстерьер - на них видно больше деталей объекта, чем на интерьере.
#
# Ключевые слова ищутся В ПУТИ, а не в имени файла: имена у застройщиков
# бессодержательны ("1.jpg", "4.jpg", "Бомба.jpg"), а папки названы по смыслу
# ("Exterior", "Living", "Bedroom", "Layout"). Порядок кортежей = порядок
# предпочтения, внутри кортежа - синонимы на разных языках.
COVER_PRIORITY = (
    # Общий план комплекса: вид сверху, панорама, генплан-визуализация.
    ("masterplan", "master plan", "master_plan", "site plan", "siteplan",
     "genplan", "генплан", "общий план", "aerial", "birdview", "bird view",
     "bird's eye", "birdseye", "overview", "panorama", "панорама", "complex",
     "комплекс"),
    # Экстерьер отдельного юнита.
    ("exterior", "экстерьер", "facade", "фасад", "outside", "ext_", "ext "),
)

# Чертежи и планировки обложкой не становятся, даже если больше ничего нет:
# это линейная графика, по ней объект не продать. Ставятся в самый конец.
COVER_DEPRIORITY = ("layout", "floor plan", "floorplan", "blueprint", "план",
                    "чертеж", "чертёж", "plan1", "plan2", "2d")


def _cover_rank(item: Dict[str, Any]) -> int:
    """
    Место файла в очереди на обложку: меньше - лучше.

    Ранги: 0..len(COVER_PRIORITY)-1 - попал в приоритетную группу;
    len(COVER_PRIORITY) - обычный рендер (интерьер и прочее);
    len(COVER_PRIORITY)+1 - чертёж/планировка, крайний случай.
    """
    haystack = f"{item.get('path', '')} {item.get('name', '')}".lower()

    for rank, keywords in enumerate(COVER_PRIORITY):
        if any(kw in haystack for kw in keywords):
            return rank

    if any(kw in haystack for kw in COVER_DEPRIORITY):
        return len(COVER_PRIORITY) + 1
    return len(COVER_PRIORITY)


def pick_cover_image(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Выбирает файл под Projects.Img из результатов зеркалирования.

    Раньше бралась просто первая скопированная картинка, то есть обложка
    зависела от порядка выдачи Drive API: у K-Village так в обложку попадал
    санузел ("SU_1/enhanced_wc cam02"). Теперь порядок задан COVER_PRIORITY.

    Возвращает элемент results или None, если годных картинок нет.
    """
    from app.doc_router import is_document_scan

    candidates = []
    for item in results or []:
        file_id = item.get("file_id") or item.get("id")
        if item.get("status") not in ("copied", "exists", "uploaded") or not file_id:
            continue
        # Скан документа не обложка: в зеркало попадает и "SLF reg.jpeg".
        if is_document_scan({
            "mimeType": "image/jpeg",
            "name": item.get("name"),
            "path": item.get("path", ""),
        }):
            continue
        candidates.append(item)

    if not candidates:
        return None

    # Сортировка стабильная, поэтому внутри одного ранга сохраняется исходный
    # порядок - обложка не меняется от прогона к прогону на одних данных.
    return min(candidates, key=_cover_rank)


def extract_mirror_airtable_fields(mirror_summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Извлекает поля Airtable из результата зеркалирования:
    - Renders: ссылка на папку-зеркало на нашем Drive;
    - Img: обложка проекта, выбранная по COVER_PRIORITY (общий план комплекса,
      иначе экстерьер).

    Именно Renders, а не 'Link to Developer’s Kit (Rus)': последнее - ссылка на
    материалы САМОГО застройщика, первоисточник. Записав туда своё зеркало, мы
    теряем оригинал и на следующем прогоне выкачиваем собственную копию вместо
    материалов застройщика. Поле Projects.Renders заведено под это 02.08.2026
    (в Units одноимённое поле было изначально).
    """
    fields: Dict[str, Any] = {}
    if not mirror_summary or not isinstance(mirror_summary, dict):
        return fields

    dest_folder_id = mirror_summary.get("dest_folder_id")
    if dest_folder_id:
        fields["Renders"] = f"https://drive.google.com/drive/folders/{dest_folder_id}"

    cover = pick_cover_image(mirror_summary.get("results") or [])
    if cover:
        # mirror_drive_image returns 'file_id', not 'id'
        file_id = cover.get("file_id") or cover.get("id")
        # Формат URL картинки закреплён каноном базы (RULES.md): sz=w2000.
        fields["Img"] = [{"url": f"https://drive.google.com/thumbnail?id={file_id}&sz=w2000"}]

    return fields
