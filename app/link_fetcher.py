import re
import logging
import urllib.parse
import httpx
import json
import os
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from google import genai
from google.genai import types

from app import content_cache
from app import llm_gate
from app.url_safety import (
    GOOGLE_HOST_PATTERNS,
    NOTION_HOST_PATTERNS,
    UnsafeUrlError,
    configured_trusted_hosts,
    read_response_limited,
    redact_url,
    stream_response_to_tempfile,
    stream_safe_url,
    validate_url_origin,
)

try:
    from app.database import save_extraction
except ImportError:
    save_extraction = None

from app.doc_parser import parse_pdf_document

logger = logging.getLogger("LinkFetcher")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
# Через llm_gate, а не по наличию ключа: см. app/llm_gate.py.
client = genai.Client(api_key=GEMINI_API_KEY) if llm_gate.llm_enabled() else None


def _bounded_int_env(name: str, default: int, maximum: int) -> int:
    """Read a positive ingress limit without letting a bad env disable it."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default
    return max(1, min(value, maximum))


MAX_DOCUMENT_DOWNLOAD_BYTES = _bounded_int_env(
    "MAX_DOCUMENT_DOWNLOAD_BYTES", 25 * 1024 * 1024, 50 * 1024 * 1024
)
MAX_SHEET_DOWNLOAD_BYTES = _bounded_int_env(
    "MAX_SHEET_DOWNLOAD_BYTES", 1 * 1024 * 1024, 5 * 1024 * 1024
)
MAX_NOTION_HTML_BYTES = _bounded_int_env(
    "MAX_NOTION_HTML_BYTES", 1 * 1024 * 1024, 5 * 1024 * 1024
)
MAX_NOTION_API_BYTES = _bounded_int_env(
    "MAX_NOTION_API_BYTES", 2 * 1024 * 1024, 8 * 1024 * 1024
)
MAX_NOTION_PAGE_CHUNKS = _bounded_int_env("MAX_NOTION_PAGE_CHUNKS", 20, 100)
MAX_NESTED_LINKS = _bounded_int_env("MAX_NESTED_LINKS", 10, 50)
MAX_SHEET_TEXT_CHARS = _bounded_int_env("MAX_SHEET_TEXT_CHARS", 15000, 100000)

SHEET_SYSTEM_PROMPT = """
Ты — эксперт по анализу шахматок недвижимости на Бали.
Перед тобой содержимое таблицы (CSV/текст), выкачанное по ссылке из чата девелопера.

Твоя задача — извлечь списки всех юнитов из шахматки:
1. Название проекта
2. Номер или имя юнита (например: Villa 101, Unit 3B)
3. Количество спален (Bedrooms)
4. Площадь в кв.м (Area)
5. Цена в USD
6. Статус (Available / Sold / Blocked / Reserved)

Верни строго JSON по схеме:
{
  "project_name": "Название проекта",
  "units": [
    {
      "unit_id": "номер юнита",
      "bedrooms": 2,
      "area_sqm": 120,
      "price_usd": 250000,
      "status": "Available / Sold / Blocked"
    }
  ]
}
"""

def extract_gsheet_id(url: str) -> Optional[str]:
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
    return match.group(1) if match else None

def extract_gdrive_id(url: str) -> Optional[str]:
    """
    Найдено 2026-08-01: без паттерна /drive/folders/ ссылка на папку Drive
    не распознаётся ВООБЩЕ - gdrive_id всегда None, ветка "Google Drive File
    or Folder" в process_generic_link() для неё никогда не срабатывает, и
    ссылка на папку проваливается через все ветки молча (пустой result без
    единого gap). Было незаметно, пока Notion возвращал пустой контент и
    вложенные ссылки на папки просто неоткуда было взять.
    """
    match = (re.search(r'/file/d/([a-zA-Z0-9-_]+)', url)
             or re.search(r'/drive/folders/([a-zA-Z0-9-_]+)', url)
             or re.search(r'[?&]id=([a-zA-Z0-9-_]+)', url))
    return match.group(1) if match else None

def is_notion_url(url: str) -> bool:
    try:
        validate_url_origin(url, allowed_hosts=NOTION_HOST_PATTERNS)
        return True
    except UnsafeUrlError:
        return False

_NOTION_ID_RE = re.compile(r'([0-9a-fA-F]{32})(?:[?#]|$)')

def extract_notion_page_id(url: str) -> Optional[str]:
    """
    Достаёт id страницы из URL и приводит к формату с дефисами (8-4-4-4-12),
    которого ждёт внутренний API Notion. Если id в пути нет (ссылка со slug
    вида domain.notion.site/elysiumgroupbali или "голая" domain.notion.site/?pvs=73),
    возвращает None - дальше пробуется resolve_notion_page_id_from_html().
    """
    path = urllib.parse.urlparse(url).path
    m = _NOTION_ID_RE.search(path + '?')
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"

# UUID в нижнем регистре: настоящие id страниц Notion всегда такие.
_NOTION_HTML_UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

# Константа, присутствующая в HTML ЛЮБОЙ страницы Notion (проверено на четырёх
# разных сайтах застройщиков - один и тот же UUID, в верхнем регистре).
# Регистром отличать нельзя: полагаться на него хрупко, поэтому исключаем явно.
_NOTION_CONSTANT_UUID = "ea76605a-f565-4b17-a496-34435622a1eb"

async def resolve_notion_page_id_from_html(url: str) -> Optional[str]:
    """
    Достаёт id страницы из HTML, когда в URL его нет.

    Notion - SPA, и раньше считалось, что её HTML это универсальная оболочка без
    единого UUID (так и есть для "голой" корневой ссылки domain.notion.site/?pvs=73).
    Но у ссылок со slug в пути (domain.notion.site/elysiumgroupbali) HTML содержит
    настоящий id страницы - проверено на живых ссылках из базы, разбор по этому id
    вернул реальный контент (4858 символов у x-hotel-nuanu). Это закрывает
    большинство нерезолвящихся ссылок без headless-браузера.

    Для по-настоящему голой корневой ссылки вернёт None: там в HTML только
    общая для всех страниц константа Notion, id взять неоткуда.
    """
    try:
        async with stream_safe_url(
            url,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0"},
            allowed_hosts=NOTION_HOST_PATTERNS,
        ) as res:
            if res.status_code != 200:
                return None
            html = (await read_response_limited(res, MAX_NOTION_HTML_BYTES)).decode(
                "utf-8", errors="replace"
            )
    except Exception as e:
        logger.warning("Could not fetch Notion HTML %s: %s", redact_url(url), e)
        return None

    for candidate in _NOTION_HTML_UUID_RE.findall(html):
        if candidate != _NOTION_CONSTANT_UUID:
            logger.info(f"🔎 id страницы Notion взят из HTML: {candidate} ({url})")
            return candidate
    return None

def _notion_rich_text_to_text_and_links(rich_text) -> Tuple[str, List[str]]:
    """Разбирает rich-text Notion: список [текст, [[метка, ...]]]. Метка 'a' - ссылка."""
    text_parts, links = [], []
    for chunk in rich_text or []:
        if not chunk:
            continue
        text_parts.append(chunk[0] if chunk[0] else '')
        for mark in (chunk[1] if len(chunk) > 1 else []) or []:
            if mark and mark[0] == 'a' and len(mark) > 1:
                links.append(mark[1])
    return ''.join(text_parts), links

def _unwrap_notion_block(blocks: dict, block_id: str) -> Optional[dict]:
    wrap = blocks.get(block_id)
    if not wrap:
        return None
    value = wrap.get('value', {})
    return value.get('value', value)

def _walk_notion_blocks(root_id: str, blocks: dict) -> Tuple[str, List[str]]:
    """
    Обходит дерево блоков от root_id в порядке чтения (глубина, а не порядок
    ключей словаря - иначе текст для модели придёт вперемешку), собирая текст
    и ссылки из rich-text меток. Работает для любого типа блока (text, quote,
    table_row, sub_header...) - у всех текст лежит в properties, просто под
    разными ключами (у table_row - случайные id колонок, не 'title').
    """
    text_lines: List[str] = []
    links: List[str] = []
    visited = set()

    def visit(block_id: str):
        if block_id in visited:
            return
        visited.add(block_id)
        v = _unwrap_notion_block(blocks, block_id)
        if not v:
            return
        for prop_value in (v.get('properties') or {}).values():
            txt, prop_links = _notion_rich_text_to_text_and_links(prop_value)
            if txt.strip():
                text_lines.append(txt.strip())
            links.extend(prop_links)
        for child_id in v.get('content') or []:
            visit(child_id)

    visit(root_id)
    return '\n'.join(text_lines), links

async def _fetch_notion_block_tree(domain: str, page_id: str) -> Optional[dict]:
    """
    Забирает ВСЕ блоки поддерева страницы через внутренний API Notion
    (loadCachedPageChunkV2), не требует авторизации на публичных страницах.

    cursor:null в ответе НЕ означает "всё загружено" - проверено эмпирически:
    первый вызов с cursor.stack=[] вернул 86 блоков и cursor:null, но 16 из 41
    прямых потомков корня отсутствовали в ответе, включая все ссылки на
    Google Drive (Brochures/Legal/Renders и т.п.). Единственный надёжный
    способ получить полное дерево - пройтись cursor.stack с нарастающим index
    (эмулируя скролл в браузере) и остановиться, когда очередной проход не
    добавил новых блоков.
    """
    url = f"https://{domain}/api/v3/loadCachedPageChunkV2"
    all_blocks: dict = {}
    space_id: Optional[str] = None

    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Mozilla/5.0"}) as http_client:
        for index in range(0, MAX_NOTION_PAGE_CHUNKS * 10, 10):
            cursor = {"stack": []} if index == 0 else {
                "stack": [[{"table": "block", "id": page_id, "index": index, "spaceId": space_id}]]
            }
            body = {"page": {"id": page_id}, "cursor": cursor, "verticalColumns": False}
            try:
                async with stream_safe_url(
                    url,
                    method="POST",
                    json_body=body,
                    timeout=20.0,
                    headers={"User-Agent": "Mozilla/5.0"},
                    allowed_hosts=NOTION_HOST_PATTERNS,
                    client=http_client,
                ) as res:
                    if res.status_code != 200:
                        break
                    raw_data = await read_response_limited(res, MAX_NOTION_API_BYTES)
                    data = json.loads(raw_data.decode("utf-8"))
            except Exception as e:
                logger.error(f"Ошибка запроса к Notion API ({domain}, index={index}): {e}")
                break

            blocks = data.get("recordMap", {}).get("block", {})
            if not blocks:
                break

            if space_id is None:
                page_block = _unwrap_notion_block(blocks, page_id)
                space_id = data.get("spaceId") or (page_block or {}).get("space_id")

            new_count = sum(1 for b in blocks if b not in all_blocks)
            all_blocks.update(blocks)

            if index > 0 and new_count == 0:
                break

    return all_blocks if page_id in all_blocks else None

def extract_nested_urls(text: str) -> List[str]:
    """Извлекает все релевантные внешние ссылки из текста/Notion (Google Drive, Sheets, PDF)."""
    urls = re.findall(r'https?://[^\s<>"\')]+', text)
    cleaned_urls = []
    for u in urls:
        u_clean = u.rstrip('.,;)]}')
        try:
            validate_url_origin(u_clean)
        except UnsafeUrlError:
            continue
        if any(kw in u_clean.lower() for kw in ['drive.google.com', 'docs.google.com', '.pdf', 'notion.site', 'notion.so']):
            if u_clean not in cleaned_urls:
                cleaned_urls.append(u_clean)
    return cleaned_urls

def _looks_like_declared_type(content: bytes, suffix: str) -> bool:
    """
    Для файлов, слишком больших для антивирусной проверки (порог у Google ~100 МБ),
    Drive отвечает статусом 200, но телом является HTML-страница подтверждения
    ("Google Drive can't scan this file for viruses..."), а не сам файл. Статус
    200 сам по себе не доказывает, что скачался настоящий документ - проверяем
    магические байты по объявленному расширению.
    """
    if suffix.lower() == ".pdf":
        return content[:5] == b"%PDF-"
    return True

async def download_file_from_url(url: str, suffix: str = ".pdf") -> Tuple[Optional[str], bool]:
    """
    Скачивает файл по публичной ссылке во временную директорию.
    Возвращает (file_path, is_private).
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    temp_path: Optional[str] = None
    try:
        async with stream_safe_url(
            url,
            timeout=30.0,
            headers=headers,
            allowed_hosts=configured_trusted_hosts(),
        ) as res:
            final_url = str(res.url)
            if (
                res.status_code in (401, 403)
                or "ServiceLogin" in final_url
                or "accounts.google.com" in final_url
            ):
                logger.warning("Private/login-required link: %s", redact_url(url))
                return None, True
            if res.status_code != 200:
                logger.warning("Download returned HTTP %s: %s", res.status_code, redact_url(url))
                return None, False

            temp_path = await stream_response_to_tempfile(
                res,
                suffix=suffix,
                max_bytes=MAX_DOCUMENT_DOWNLOAD_BYTES,
            )

        with open(temp_path, "rb") as downloaded_file:
            signature = downloaded_file.read(32)
        if not _looks_like_declared_type(signature, suffix):
            logger.warning("Downloaded content does not match expected %s: %s", suffix, redact_url(url))
            os.remove(temp_path)
            return None, False
        return temp_path, False
    except UnsafeUrlError as exc:
        logger.warning("Rejected unsafe or oversized download %s: %s", redact_url(url), exc)
    except Exception as exc:
        logger.error("Download failed for %s: %s", redact_url(url), exc)

    if temp_path and os.path.exists(temp_path):
        os.remove(temp_path)
    return None, False

async def fetch_notion_content(url: str) -> Tuple[Optional[str], List[str], bool]:
    """
    Читает страницу Notion через внутренний API (loadCachedPageChunkV2), не браузером.
    Возвращает (clean_text, nested_urls, is_private).

    Обычный httpx.get() на Notion возвращает пустую JS-заглушку - страница рендерится
    скриптом на клиенте, серверный HTML контента не содержит (проверено вручную на
    реальной ссылке: 95 символов "Notion JavaScript must be enabled..."). Внутренний
    API того же React-приложения отвечает 200 без авторизации на публичных страницах.

    Если id нет в пути (ссылка со slug), он ищется в HTML - см.
    resolve_notion_page_id_from_html(). Не резолвится только по-настоящему "голая"
    корневая ссылка (domain.notion.site/?pvs=73): в её HTML нет ни одного UUID, кроме
    общей для всех страниц константы. Для неё остаётся браузерный фолбэк (Э1a, не
    реализован); здесь возвращается is_private=False с пустым текстом, и вызывающий код
    обязан зафиксировать это в Gaps, а не молча продолжить как будто источник прочитан.
    """
    domain = urllib.parse.urlparse(url).netloc
    page_id = extract_notion_page_id(url) or await resolve_notion_page_id_from_html(url)
    if not page_id:
        logger.warning(f"⚠️ Notion-ссылка без id страницы ни в пути, ни в HTML: {url}")
        return None, [], False

    try:
        blocks = await _fetch_notion_block_tree(domain, page_id)
    except Exception as e:
        logger.error(f"Ошибка чтения Notion страницы {url}: {e}")
        return None, [], False

    if not blocks:
        logger.warning(f"🔒 Страница Notion недоступна или не существует: {url}")
        return None, [], True

    clean_text, links = _walk_notion_blocks(page_id, blocks)
    nested = []
    for link in links:
        if link not in nested and any(
            kw in link.lower() for kw in ['drive.google.com', 'docs.google.com', '.pdf', 'notion.site', 'notion.so']
        ):
            nested.append(link)

    return clean_text, nested, False

async def persist_parsed_result(parsed: Optional[Dict[str, Any]], message_id: int, chat_id: int, url: str):
    """
    Сохраняет результат разбора Drive PDF / Notion в ту же очередь extractions,
    что и обычные текстовые сообщения (см. app/listener.py). Без этого шага
    process_generic_link честно парсит документ и тут же теряет результат -
    вызывающий код (listener.py, history_scanner.py) его не сохраняет.
    """
    if not parsed or not parsed.get("is_relevant") or not save_extraction:
        return
    proj_data = parsed.get("Projects", {}) or {}
    project_name = proj_data.get("Project Name") or "UNKNOWN"
    await save_extraction(
        message_id=message_id,
        chat_id=chat_id,
        project_recid=project_name,
        object_guess=f"Parsed from link: {url}",
        confidence=parsed.get("confidence", 0.8),
        slot="link_fetch",
        url_status="parsed",
        why=parsed.get("reason", ""),
        needs_human=True,
        raw_json=parsed,
    )

async def notify_admin_private_link(url: str, chat_title: Optional[str] = None):
    """Отправляет уведомление администратору в Telegram при обнаружении закрытой/приватной ссылки."""
    alert_token = os.environ.get('ALERT_BOT_TOKEN')
    alert_chat_id = os.environ.get('ALERT_CHAT_ID')
    if alert_token and alert_chat_id:
        msg = f"🔒 **ВНИМАНИЕ: Найдена приватная/закрытая ссылка!**\nЧат: {chat_title or 'Неизвестно'}\nСсылка: {url}\nЗапросите доступ у девелопера!"
        try:
            url_api = f"https://api.telegram.org/bot{alert_token}/sendMessage"
            payload = {"chat_id": alert_chat_id, "text": msg, "parse_mode": "Markdown"}
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url_api, json=payload)
        except Exception as e:
            logger.error(f"Не удалось отправить алерт о закрытой ссылке: {e}")

async def process_generic_link(
    url: str,
    message_id: int = 0,
    chat_id: int = 0,
    chat_title: Optional[str] = None,
    depth: int = 0,
    max_depth: int = 2,
    visited: Optional[set] = None,
    project_name: Optional[str] = None,
    exclude_fields: Optional[set] = None,
    max_nested_links: int = MAX_NESTED_LINKS,
) -> Dict[str, Any]:
    """
    Единая точка входа для обработки любых внешних ссылок.
    Поддерживает: Google Drive (PDF/папки), Google Sheets, Notion (с защитой от циклов/лимитом глубины), Yandex.Disk, Dropbox, Прямые PDF.

    project_name (Э6, implementation_plan.md): если передан и ссылка ведёт на
    папку Drive, найденные изображения зеркалируются на личный Drive владельца.
    Без project_name (звонок из истории/без разобранной карточки) зеркалирование
    просто не запускается - оно требует места назначения, гадать имя проекта нельзя.

    exclude_fields (владелец, 02.08.2026): поля, уже закрытые шахматкой,
    обработанной раньше этой ссылки в рамках того же проекта - документы Drive
    под них не открываются, см. doc_pipeline.fill_fields_from_drive_files.
    """
    if visited is None:
        visited = set()
        
    url_clean = (url or "").strip()
    result = {
        "url": url_clean,
        "is_private": False,
        "nested_urls": [],
        "parsed_data": None,
        "dev_kit_url": None,
        "drive_files": None,
        "gaps": [],
    }
    if url_clean in visited or depth > max_depth:
        logger.warning(f"⚠️ Превышен лимит глубины ({depth}/{max_depth}) или повторный переход по ссылке: {url_clean}")
        result["gaps"].append("Cycle or max depth reached")
        return result

    try:
        validate_url_origin(url_clean)
    except UnsafeUrlError as exc:
        logger.warning("Rejected URL before processing %s: %s", redact_url(url_clean), exc)
        result["gaps"].append(f"URL rejected by security policy: {exc}")
        return result

    visited.add(url_clean)
    logger.info("Processing external link (depth %s): %s", depth, redact_url(url_clean))

    # 1. Google Sheets - шахматка сканируется ПЕРВОЙ (владелец, 02.08.2026):
    # её принятые предложения возвращаются вызывающему как doc_findings,
    # чтобы дальнейшее открытие документов Drive под тот же проект пропускало
    # уже закрытые ею поля (см. exclude_fields у process_generic_link).
    gsheet_id = extract_gsheet_id(url_clean)
    if gsheet_id:
        sheet_findings = await fetch_and_parse_link(
            url_clean, message_id, chat_id, project_name=project_name
        )
        result["dev_kit_url"] = url_clean
        if sheet_findings:
            result["doc_findings"] = sheet_findings
            result["gaps"].extend(sheet_findings.get("gaps", []))
        return result

    # 2. Google Drive File or Folder
    gdrive_id = extract_gdrive_id(url_clean)
    if gdrive_id:
        if "/drive/folders/" in url_clean:
            result["dev_kit_url"] = url_clean
            try:
                from app.drive_folder import list_drive_folder_recursive
                files = await asyncio.to_thread(list_drive_folder_recursive, gdrive_id)
                result["drive_files"] = files
                logger.info(f"📁 Папка Google Drive развёрнута: {url_clean} -> {len(files)} файлов")

                if project_name:
                    from app.drive_mirror import extract_mirror_airtable_fields, mirror_listed_drive_folder
                    try:
                        mirror_summary = await asyncio.to_thread(
                            mirror_listed_drive_folder, gdrive_id, files, project_name
                        )
                        result["drive_mirror"] = mirror_summary
                        result["mirror_airtable_fields"] = extract_mirror_airtable_fields(mirror_summary)
                        result["gaps"].extend(mirror_summary.get("gaps", []))
                        copied = sum(1 for r in mirror_summary["results"] if r["status"] == "copied")
                        logger.info(
                            f"🪞 Зеркало на личный Drive: {copied} скопировано "
                            f"из {url_clean} -> {project_name}"
                        )
                    except RuntimeError as mirror_err:
                        # То же ожидаемое состояние "OAuth ещё не настроен", что и
                        # у листинга ниже - зеркалирование не должно валить обработку
                        # ссылки целиком, только своё собственное отсутствие.
                        logger.warning(f"⚠️ Зеркалирование на Drive недоступно ({mirror_err}): {url_clean}")
                        result["gaps"].append(f"Google Drive mirror unavailable: {url_clean}")
                    except Exception as mirror_err:
                        logger.error(f"Ошибка зеркалирования Google Drive {url_clean}: {mirror_err}")
                        result["gaps"].append(f"Google Drive mirror failed: {url_clean}")

                    # Э2: разбор документов под ПУСТЫЕ поля карточки. Предложения
                    # никуда не записываются - их судьбу решает Confirmed (Э4).
                    try:
                        from app.doc_pipeline import run_for_project
                        docs = await run_for_project(project_name, files, exclude_fields=exclude_fields)
                        if docs:
                            result["doc_findings"] = docs
                            result["gaps"].extend(docs.get("gaps", []))
                            logger.info(
                                f"📄 Документы {url_clean}: {len(docs['proposals'])} предложений "
                                f"по пустым полям, открыто {docs['opened']}"
                            )
                    except Exception as doc_err:
                        logger.error(f"Ошибка разбора документов из {url_clean}: {doc_err}")
                        result["gaps"].append(f"Document parsing failed: {url_clean}")
            except RuntimeError as e:
                # OAuth к личному Drive ещё не настроен (tools_drive_auth_setup.py
                # не запускали) - это ожидаемое состояние до Э6, не повод падать.
                logger.warning(f"⚠️ Листинг папки Drive недоступен ({e}): {url_clean}")
                result["gaps"].append(f"Google Drive folder listing unavailable: {url_clean}")
            except Exception as e:
                logger.error(f"Ошибка листинга папки Google Drive {url_clean}: {e}")
                result["gaps"].append(f"Google Drive folder listing failed: {url_clean}")
            return result

        direct_url = f"https://docs.google.com/uc?export=download&id={gdrive_id}"
        file_path, is_private = await download_file_from_url(direct_url, suffix=".pdf")
        if is_private:
            result["is_private"] = True
            result["gaps"].append(f"Private Google Drive Link: {url_clean}")
            await notify_admin_private_link(url_clean, chat_title)
            return result
            
        if file_path and os.path.exists(file_path):
            try:
                parsed = await parse_pdf_document(file_path, chat_title=chat_title)
                result["parsed_data"] = parsed
                result["dev_kit_url"] = url_clean
                await persist_parsed_result(parsed, message_id, chat_id, url_clean)
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
            return result

        # gdrive_id распознан, но файл не скачался (не приватность - это уже
        # обработано выше) - без явного gap вызывающий код проваливается через
        # все следующие ветки и получает пустой result без единого объяснения.
        result["gaps"].append(f"Google Drive file could not be downloaded: {url_clean}")
        return result

    # 3. Notion Page
    if is_notion_url(url_clean):
        clean_text, nested_urls, is_private = await fetch_notion_content(url_clean)
        if is_private:
            result["is_private"] = True
            result["gaps"].append(f"Private Notion Page: {url_clean}")
            await notify_admin_private_link(url_clean, chat_title)
            return result
            
        result["nested_urls"] = nested_urls
        result["dev_kit_url"] = url_clean
        
        if clean_text and len(clean_text) > 50:
            from app.gemini_parser import parse_message
            parsed = await parse_message(clean_text[:30000], chat_title=chat_title)
            result["parsed_data"] = parsed
            await persist_parsed_result(parsed, message_id, chat_id, url_clean)
        elif not is_private:
            # Не приватность и не ошибка сети - просто нечего было прочитать
            # (чаще всего "голая" корневая ссылка без id страницы в пути,
            # extract_notion_page_id вернул None). Без явного gap вызывающий
            # код решит, что источник обработан, хотя из него ничего не взято.
            result["gaps"].append(f"Notion page could not be read (no page id or empty content): {url_clean}")

        # Рекурсивная обработка вложенных ссылок с защитой от циклов.
        # Раньше здесь стоял break после первой подошедшей ссылки - пока Notion
        # парсился в пустышку (см. fetch_notion_content), это было незаметно:
        # nested_urls всегда были пустыми. Теперь страница реально отдаёт до
        # 7+ ссылок на папки Drive (Brochures/Legal/Renders/...), и break отбрасывал
        # бы все, кроме первой. Папки возвращаются мгновенно (ветка 2 не скачивает
        # их содержимое), так что обход всех ссылок дёшев.
        try:
            nested_limit = max(1, min(int(max_nested_links), MAX_NESTED_LINKS))
        except (TypeError, ValueError):
            nested_limit = MAX_NESTED_LINKS
        if len(nested_urls) > nested_limit:
            result["gaps"].append(
                f"Nested URL limit reached: processed {nested_limit} of {len(nested_urls)} links"
            )

        for nested in nested_urls[:nested_limit]:
            if nested in visited:
                continue
            if not (extract_gdrive_id(nested) or nested.lower().endswith('.pdf') or is_notion_url(nested)):
                continue
            logger.info(f"🔗 Переходим по вложенной ссылке из Notion: {nested} (Глубина {depth + 1})")
            nested_res = await process_generic_link(
                nested, message_id, chat_id, chat_title,
                depth=depth + 1, max_depth=max_depth, visited=visited,
                project_name=project_name,
                max_nested_links=nested_limit,
            )
            if nested_res.get("parsed_data"):
                result["parsed_data"] = nested_res["parsed_data"]
            result["gaps"].extend(nested_res.get("gaps", []))
            if nested_res.get("is_private"):
                result["is_private"] = True
            if nested_res.get("drive_mirror"):
                result.setdefault("drive_mirror_nested", []).append(nested_res["drive_mirror"])
        return result

    # 4. Яндекс.Диск / Dropbox / Прямая ссылка на PDF
    is_yandex_or_dropbox = any(kw in url_clean.lower() for kw in ['yadi.sk', 'disk.yandex', 'dropbox.com'])
    if url_clean.lower().endswith('.pdf') or is_yandex_or_dropbox:
        file_path, is_private = await download_file_from_url(url_clean, suffix=".pdf")
        if is_private:
            result["is_private"] = True
            result["gaps"].append(f"Private File Link: {url_clean}")
            await notify_admin_private_link(url_clean, chat_title)
            return result
            
        if file_path and os.path.exists(file_path):
            try:
                parsed = await parse_pdf_document(file_path, chat_title=chat_title)
                result["parsed_data"] = parsed
                result["dev_kit_url"] = url_clean
                await persist_parsed_result(parsed, message_id, chat_id, url_clean)
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
            return result

        result["gaps"].append(f"File could not be downloaded: {url_clean}")
        return result

    return result

class _BoundedSheetResponse:
    """Small response adapter used by the existing Google Sheet parser."""

    def __init__(self, status_code: int, url: str, text: str):
        self.status_code = status_code
        self.url = url
        self.text = text


class _SafeGoogleSheetClient:
    """Expose a tiny ``get`` API while enforcing URL and byte limits."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str) -> _BoundedSheetResponse:
        async with stream_safe_url(
            url,
            timeout=15.0,
            allowed_hosts=GOOGLE_HOST_PATTERNS,
        ) as response:
            final_url = str(response.url)
            is_private = (
                response.status_code in (401, 403)
                or "ServiceLogin" in final_url
                or "accounts.google.com" in final_url
            )
            content = b"" if is_private or response.status_code != 200 else await read_response_limited(
                response, MAX_SHEET_DOWNLOAD_BYTES
            )
            return _BoundedSheetResponse(
                response.status_code,
                final_url,
                content.decode("utf-8", errors="replace"),
            )


def _normalise_sheet_availability(value: Any) -> Optional[str]:
    """Map only unambiguous source statuses to the Airtable select canon."""
    normalised = str(value or "").strip().lower()
    if not normalised:
        return None
    if "sold" in normalised:
        return "Sold"
    if normalised in {"available", "on sale", "for sale", "sale"}:
        return "On sale"
    if normalised == "blocked":
        return "Blocked"
    if normalised == "resale":
        # Живая опция базы не различает "от застройщика" и "перепродажа
        # инвестором" - юнит по-прежнему реально продаётся (владелец,
        # 05.08.2026, кейс K-Village Villa 12A). Помечаем откуда взялся
        # статус, чтобы происхождение не терялось молча.
        return "On sale"
    return None


def _sheet_payload_for_sync(
    parsed_sheet: Dict[str, Any], fallback_project_name: Optional[str]
) -> Tuple[Dict[str, Any], str, List[str]]:
    """Convert the sheet model output into one durable sync-queue payload.

    Storing one aggregate payload is important: the sync worker expects a
    complete ``Projects``/``Units`` document, while a staging row per unit
    without ``raw_json`` is never eligible for a durable Airtable export.
    """
    source_project = str(parsed_sheet.get("project_name") or "").strip()
    fallback = str(fallback_project_name or "").strip()
    project_name = source_project if source_project and source_project.lower() != "unknown project" else fallback
    if not project_name:
        # The row is deliberately marked for human review.  A non-empty name
        # keeps the aggregate payload structurally valid and prevents units
        # from becoming orphan writes in the sync worker.
        project_name = "Unknown Project"

    normalised_units: List[Dict[str, Any]] = []
    gaps: List[str] = []
    for index, raw_unit in enumerate(parsed_sheet.get("units") or [], start=1):
        if not isinstance(raw_unit, dict):
            gaps.append(f"Sheet unit {index} was not an object and was skipped")
            continue

        unit_number = str(raw_unit.get("unit_id") or raw_unit.get("unit_number") or "").strip()
        unit_type = str(
            raw_unit.get("unit_type") or raw_unit.get("type") or unit_number or "Unspecified"
        ).strip()
        unit: Dict[str, Any] = {"Unit type": unit_type}
        if unit_number:
            unit["Unit Number"] = unit_number

        field_map = {
            "bedrooms": "Bedrooms",
            "area_sqm": "Area from (m2)",
            "price_usd": "Price from (USD)",
        }
        for source_key, target_key in field_map.items():
            value = raw_unit.get(source_key)
            if value not in (None, ""):
                unit[target_key] = value

        raw_status = raw_unit.get("status")
        availability = _normalise_sheet_availability(raw_status)
        if availability:
            unit["Availability"] = availability
            if str(raw_status or "").strip().lower() == "resale":
                gaps.append(
                    f"Sheet unit {unit_number or index}: resale from a previous buyer, "
                    f"not a fresh developer unit"
                )
        elif raw_unit.get("status") not in (None, ""):
            gaps.append(
                f"Sheet unit {unit_number or index}: unrecognised availability {raw_unit.get('status')!r}"
            )
        normalised_units.append(unit)

    payload: Dict[str, Any] = {
        "Developer": {},
        "Projects": {"Project Name": project_name},
        "Units": normalised_units,
        "Gaps": gaps,
    }
    return payload, project_name, gaps


async def fetch_and_parse_link(
    url: str, message_id: int, chat_id: int, project_name: Optional[str] = None
):
    """
    Переходит по ссылке Google Sheets, выкачивает содержимое и парсит шахматку
    через Gemini (список юнитов), а если передан project_name - ещё и полями
    карточки проекта (район, срок сдачи, форма владения и т.п.) через
    doc_pipeline.run_for_project_from_sheet.

    Возвращает summary извлечения полей карточки ({proposals, gaps, opened})
    или None, если project_name не передан либо ничего не нашлось.
    """
    try:
        validate_url_origin(url, allowed_hosts=GOOGLE_HOST_PATTERNS)
    except UnsafeUrlError as exc:
        logger.warning("Rejected Google Sheet URL %s: %s", redact_url(url), exc)
        return None

    gsheet_id = extract_gsheet_id(url)
    if not gsheet_id:
        return None

    # gid обязателен: без него всегда скачивается вкладка Google по умолчанию,
    # а не та, что указана в ссылке - на реальной шахматке Mångata с несколькими
    # вкладками (townhouses/villas) это молча теряло вкладку целиком (02.08.2026).
    gid_match = re.search(r"[#&?]gid=(\d+)", url)
    export_csv_url = f"https://docs.google.com/spreadsheets/d/{gsheet_id}/export?format=csv"
    if gid_match:
        export_csv_url += f"&gid={gid_match.group(1)}"
    logger.info(f"🌐 Fetching Google Sheet CSV from: {export_csv_url}")

    sheet_findings = None

    async with _SafeGoogleSheetClient() as http_client:
        try:
            res = await http_client.get(export_csv_url)
            if res.status_code in (401, 403) or "ServiceLogin" in str(res.url):
                logger.warning(f"🔒 Link access denied (Private Sheet): {url}")
                if save_extraction:
                    await save_extraction(
                        message_id=message_id,
                        chat_id=chat_id,
                        project_recid="ACCESS_DENIED",
                        object_guess="Google Sheet",
                        confidence=1.0,
                        slot="url_access",
                        url_status="private",
                        why=f"Ссылка закрыта настройками приватности: {url}",
                        needs_human=True
                    )
                return None

            if res.status_code == 200:
                csv_text = res.text[:MAX_SHEET_TEXT_CHARS]
                logger.info(f"Successfully downloaded Google Sheet CSV ({len(csv_text)} bytes).")

                # Поля карточки (район, срок сдачи, форма владения...) - из
                # той же шахматки, независимо от того, распознались ли юниты
                # ниже. Владелец, 02.08.2026: шахматка сканируется первой.
                if project_name:
                    try:
                        from app.doc_pipeline import run_for_project_from_sheet
                        sheet_findings = await run_for_project_from_sheet(
                            project_name, csv_text, source_name=f"шахматка: {url}"
                        )
                    except Exception as e:
                        logger.error(f"Ошибка извлечения полей карточки из шахматки {url}: {e}")

                # Одну и ту же шахматку слушатель встречает повторно — на
                # каждом рескане истории чата и при каждом повторном упоминании
                # ссылки. Ключ по содержимому CSV, без привязки к чату:
                # SHEET_SYSTEM_PROMPT статичен и не зависит от контекста.
                cache_key = content_cache.hash_text(csv_text)
                parsed_sheet = await asyncio.to_thread(content_cache.get, cache_key)
                if parsed_sheet is not None:
                    logger.info(f"💾 Cache hit for Google Sheet {gsheet_id}, skipping Gemini call")
                elif client:
                    logger.info(f"Cache miss for Google Sheet {gsheet_id}, parsing with Gemini...")
                    from app.gemini_parser import resolve_model_name
                    response = await client.aio.models.generate_content(
                        model=resolve_model_name(),
                        contents=f"Вот содержимое таблицы:\n\n{csv_text}",
                        config=types.GenerateContentConfig(
                            system_instruction=SHEET_SYSTEM_PROMPT,
                            response_mime_type="application/json",
                            temperature=0.1
                        )
                    )

                    text_resp = response.text.strip()
                    if text_resp.startswith("```"):
                        text_resp = re.sub(r"^```(?:json)?\n?|```$", "", text_resp).strip()

                    parsed_sheet = json.loads(text_resp)
                    if content_cache.is_cacheable(parsed_sheet):
                        await asyncio.to_thread(content_cache.put, cache_key, 'sheet', parsed_sheet)
                else:
                    parsed_sheet = None

                if parsed_sheet:
                    # Имя проекта, как его написал застройщик ВНУТРИ шахматки -
                    # не путать с параметром project_name (реальным именем
                    # карточки), от которого зависит извлечение полей выше.
                    if not isinstance(parsed_sheet, dict):
                        logger.warning(
                            "Google Sheet parser returned %s instead of an object",
                            type(parsed_sheet).__name__,
                        )
                        return sheet_findings

                    raw_payload, sheet_project_name, payload_gaps = _sheet_payload_for_sync(
                        parsed_sheet, project_name
                    )
                    units = raw_payload["Units"]

                    logger.info(f"🎯 Extracted {len(units)} units from Google Sheet for project '{sheet_project_name}'!")
                    if save_extraction:
                        await save_extraction(
                            message_id=message_id,
                            chat_id=chat_id,
                            project_recid=sheet_project_name,
                            object_guess=f"Google Sheet aggregate: {len(units)} unit(s)",
                            confidence=0.95,
                            slot="unit_price",
                            url_status="parsed",
                            why=(
                                f"Google Sheet parsed into {len(units)} unit(s)"
                                + (f"; {len(payload_gaps)} normalisation gap(s)" if payload_gaps else "")
                            ),
                            needs_human=True,
                            raw_json=raw_payload,
                        )
        except Exception as e:
            logger.error(f"Error fetching/parsing Google Sheet {url}: {e}")

    return sheet_findings
