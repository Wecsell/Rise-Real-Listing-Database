import re
import logging
import urllib.parse
import httpx
import json
import os
import tempfile
from typing import Dict, Any, List, Optional, Tuple
from google import genai
from google.genai import types

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
    match = re.search(r'/file/d/([a-zA-Z0-9-_]+)', url) or re.search(r'[?&]id=([a-zA-Z0-9-_]+)', url)
    return match.group(1) if match else None

def is_notion_url(url: str) -> bool:
    domain = urllib.parse.urlparse(url).netloc.lower()
    return 'notion.site' in domain or 'notion.so' in domain

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
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                temp_file.write(res.content)
                temp_file.close()
                return temp_file.name, False
        except Exception as e:
            logger.error(f" Ошибка скачивания файла {url}: {e}")
    return None, False

async def fetch_notion_content(url: str) -> Tuple[Optional[str], List[str], bool]:
    """
    Выкачивает страницу Notion, извлекает её текстовое содержимое и вложенные ссылки.
    Возвращает (clean_text, nested_urls, is_private).
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0, headers=headers) as http_client:
        try:
            res = await http_client.get(url)
            if res.status_code in (401, 403) or "login" in str(res.url).lower():
                return None, [], True
                
            if res.status_code == 200:
                html_text = res.text
                # Извлекаем вложенные ссылки из HTML (href="...")
                raw_hrefs = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_text)
                nested = extract_nested_urls(html_text)
                for href in raw_hrefs:
                    if href not in nested and any(kw in href.lower() for kw in ['drive.google.com', 'docs.google.com', '.pdf']):
                        nested.append(href)
                        
                # Очищаем HTML теги для получения текста Notion
                clean_text = re.sub(r'<script.*?>.*?</script>', '', html_text, flags=re.DOTALL)
                clean_text = re.sub(r'<style.*?>.*?</style>', '', clean_text, flags=re.DOTALL)
                clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
                clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                
                return clean_text, nested, False
        except Exception as e:
            logger.error(f"Ошибка чтения Notion страницы {url}: {e}")
    return None, [], False

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
    visited: Optional[set] = None
) -> Dict[str, Any]:
    """
    Единая точка входа для обработки любых внешних ссылок.
    Поддерживает: Google Drive (PDF/папки), Google Sheets, Notion (с защитой от циклов/лимитом глубины), Yandex.Disk, Dropbox, Прямые PDF.
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
            logger.info(f"📁 Обнаружена папка Google Drive: {url_clean}. Помечаем Dev Kit ссылку папки.")
            result["dev_kit_url"] = url_clean
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
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
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
            
        # Рекурсивная обработка вложенных ссылок с защитой от циклов
        for nested in nested_urls:
            if nested not in visited:
                if extract_gdrive_id(nested) or nested.lower().endswith('.pdf') or is_notion_url(nested):
                    logger.info(f"🔗 Переходим по вложенной ссылке из Notion: {nested} (Глубина {depth + 1})")
                    nested_res = await process_generic_link(
                        nested, message_id, chat_id, chat_title, 
                        depth=depth + 1, max_depth=max_depth, visited=visited
                    )
                    if nested_res.get("parsed_data"):
                        result["parsed_data"] = nested_res["parsed_data"]
                    if nested_res.get("is_private"):
                        result["is_private"] = True
                        result["gaps"].extend(nested_res.get("gaps", []))
                    break
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
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
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
                logger.info(f"Successfully downloaded Google Sheet CSV ({len(csv_text)} bytes). Parsing with Gemini...")
                
                if client:
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
