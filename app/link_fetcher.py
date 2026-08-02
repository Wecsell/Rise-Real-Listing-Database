import re
import logging
import urllib.parse
import httpx
import json
import os
import asyncio
import tempfile
from typing import Dict, Any, List, Optional, Tuple
from google import genai
from google.genai import types

from app import content_cache

try:
    from app.database import save_extraction
except ImportError:
    save_extraction = None

from app.doc_parser import parse_pdf_document

logger = logging.getLogger("LinkFetcher")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

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
    domain = urllib.parse.urlparse(url).netloc.lower()
    return 'notion.site' in domain or 'notion.so' in domain

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
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            res = await client.get(url)
        if res.status_code != 200:
            return None
    except Exception as e:
        logger.warning(f"Не удалось загрузить HTML страницы Notion {url}: {e}")
        return None

    for candidate in _NOTION_HTML_UUID_RE.findall(res.text):
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
        for index in range(0, 401, 10):
            cursor = {"stack": []} if index == 0 else {
                "stack": [[{"table": "block", "id": page_id, "index": index, "spaceId": space_id}]]
            }
            body = {"page": {"id": page_id}, "cursor": cursor, "verticalColumns": False}
            try:
                res = await http_client.post(url, json=body)
            except Exception as e:
                logger.error(f"Ошибка запроса к Notion API ({domain}, index={index}): {e}")
                break

            if res.status_code != 200:
                break

            data = res.json()
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
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers=headers) as http_client:
        try:
            res = await http_client.get(url)
            if res.status_code in (401, 403) or "ServiceLogin" in str(res.url) or "accounts.google.com" in str(res.url):
                logger.warning(f"🔒 Доступ к ссылке ограничен (Private/Login required): {url}")
                return None, True

            if res.status_code == 200:
                if not _looks_like_declared_type(res.content, suffix):
                    logger.warning(
                        f"⚠️ Ответ 200, но содержимое не похоже на {suffix} - вероятно, "
                        f"интерстишл антивирусной проверки Google Drive для крупного файла: {url}"
                    )
                    return None, False
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                temp_file.write(res.content)
                temp_file.close()
                return temp_file.name, False
        except Exception as e:
            logger.error(f" Ошибка скачивания файла {url}: {e}")
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
) -> Dict[str, Any]:
    """
    Единая точка входа для обработки любых внешних ссылок.
    Поддерживает: Google Drive (PDF/папки), Google Sheets, Notion (с защитой от циклов/лимитом глубины), Yandex.Disk, Dropbox, Прямые PDF.

    project_name (Э6, implementation_plan.md): если передан и ссылка ведёт на
    папку Drive, найденные изображения зеркалируются на личный Drive владельца.
    Без project_name (звонок из истории/без разобранной карточки) зеркалирование
    просто не запускается - оно требует места назначения, гадать имя проекта нельзя.
    """
    if visited is None:
        visited = set()
        
    url_clean = url.strip()
    if url_clean in visited or depth > max_depth:
        logger.warning(f"⚠️ Превышен лимит глубины ({depth}/{max_depth}) или повторный переход по ссылке: {url_clean}")
        return {"url": url_clean, "is_private": False, "nested_urls": [], "parsed_data": None, "gaps": ["Cycle or max depth reached"]}
        
    visited.add(url_clean)
    logger.info(f"🔍 Анализируем внешнюю ссылку (Глубина {depth}): {url_clean}")
    
    result = {
        "url": url_clean,
        "is_private": False,
        "nested_urls": [],
        "parsed_data": None,
        "dev_kit_url": None,
        "drive_files": None,
        "gaps": []
    }

    # 1. Google Sheets
    gsheet_id = extract_gsheet_id(url_clean)
    if gsheet_id:
        await fetch_and_parse_link(url_clean, message_id, chat_id)
        result["dev_kit_url"] = url_clean
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
                        docs = await run_for_project(project_name, files)
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
        for nested in nested_urls:
            if nested in visited:
                continue
            if not (extract_gdrive_id(nested) or nested.lower().endswith('.pdf') or is_notion_url(nested)):
                continue
            logger.info(f"🔗 Переходим по вложенной ссылке из Notion: {nested} (Глубина {depth + 1})")
            nested_res = await process_generic_link(
                nested, message_id, chat_id, chat_title,
                depth=depth + 1, max_depth=max_depth, visited=visited,
                project_name=project_name,
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

async def fetch_and_parse_link(url: str, message_id: int, chat_id: int):
    """Переходит по ссылке Google Sheets, выкачивает содержимое и парсит шахматку через Gemini."""
    gsheet_id = extract_gsheet_id(url)
    if not gsheet_id:
        return

    export_csv_url = f"https://docs.google.com/spreadsheets/d/{gsheet_id}/export?format=csv"
    logger.info(f"🌐 Fetching Google Sheet CSV from: {export_csv_url}")

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as http_client:
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
                return
                
            if res.status_code == 200:
                csv_text = res.text[:15000]
                logger.info(f"Successfully downloaded Google Sheet CSV ({len(csv_text)} bytes).")

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
                    project_name = parsed_sheet.get("project_name", "Unknown Project")
                    units = parsed_sheet.get("units", [])

                    logger.info(f"🎯 Extracted {len(units)} units from Google Sheet for project '{project_name}'!")
                    if save_extraction and isinstance(units, list):
                        for unit in units:
                            if isinstance(unit, dict):
                                await save_extraction(
                                    message_id=message_id,
                                    chat_id=chat_id,
                                    project_recid=project_name,
                                    object_guess=f"{unit.get('unit_id')} ({unit.get('bedrooms')} BR)",
                                    confidence=0.95,
                                    slot="unit_price",
                                    url_status="parsed",
                                    why=f"Price: {unit.get('price_usd')}$, Status: {unit.get('status')}",
                                    needs_human=True
                                )
        except Exception as e:
            logger.error(f"Error fetching/parsing Google Sheet {url}: {e}")
