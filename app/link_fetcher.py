import re
import logging
import urllib.parse
import httpx
import json
import os
from google import genai
from google.genai import types

from database import save_extraction

logger = logging.getLogger("LinkFetcher")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

SHEET_SYSTEM_PROMPT = """
Ты — эксперт по анализу шахматок недвижимости на Бали.
Перед тобой содержимое таблицы (CSV/текст), выкачанное по ссылке из чата девелопера.

Твоя задача — извлечь списки всех юнитов из шахматки:
1. Название проекта
2. Номер или имя юнита (например: Villa 101, Unit 3B)
3. Количетво спален (Bedrooms)
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

def extract_gsheet_id(url: str) -> str | None:
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
    return match.group(1) if match else None

async def fetch_and_parse_link(url: str, message_id: int, chat_id: int):
    """Переходит по ссылке (Google Sheets / Web), выкачивает содержимое и парсит шахматку через Gemini."""
    gsheet_id = extract_gsheet_id(url)
    
    if not gsheet_id:
        logger.info(f"URL {url} is not a standard Google Sheet. Skipping direct CSV export.")
        return

    export_csv_url = f"https://docs.google.com/spreadsheets/d/{gsheet_id}/export?format=csv"
    logger.info(f"🌐 Fetching Google Sheet CSV from: {export_csv_url}")

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as http_client:
        try:
            res = await http_client.get(export_csv_url)
            
            if res.status_code == 403 or "ServiceLogin" in res.url.path:
                logger.warning(f"🔒 Link access denied (Private Sheet): {url}")
                # Сохраняем информацию о закрытом доступе
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
                csv_text = res.text[:15000] # Берем первые 15кб таблицы
                logger.info(f"Successfully downloaded Google Sheet CSV ({len(csv_text)} bytes). Parsing with Gemini...")
                
                # Парсим шахматку через Gemini
                if client:
                    response = await client.aio.models.generate_content(
                        model='gemini-2.0-flash',
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
                    
                    if isinstance(units, list):
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
