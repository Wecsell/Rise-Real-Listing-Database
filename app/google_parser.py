import os
import re
import io
import json
import logging
import requests
import pandas as pd
from typing import Optional, Dict, Any, List
from app.gemini_parser import client, resolve_model_name, ParsedExtraction

logger = logging.getLogger("GoogleParser")

def convert_google_sheet_url_to_csv(url: str) -> Optional[str]:
    """
    Конвертирует ссылку Google Sheets вида:
    https://docs.google.com/spreadsheets/d/{ID}/edit#gid=123
    в экспортную ссылку скачивания CSV:
    https://docs.google.com/spreadsheets/d/{ID}/export?format=csv
    """
    if not url or "docs.google.com/spreadsheets" not in url:
        return None
        
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
    if not match:
        return None
        
    sheet_id = match.group(1)
    
    # Проверяем наличия gid (конкретного листа)
    gid_match = re.search(r'[#&?]gid=(\d+)', url)
    if gid_match:
        gid = gid_match.group(1)
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
        
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"

def convert_google_drive_url_to_direct_download(url: str) -> Optional[str]:
    """
    Конвертирует ссылку Google Drive вида:
    https://drive.google.com/file/d/{ID}/view
    в прямую ссылку на скачивание файла:
    https://drive.google.com/uc?export=download&id={ID}
    """
    if not url or "drive.google.com" not in url:
        return None
        
    match = re.search(r'/file/d/([a-zA-Z0-9-_]+)', url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
        
    id_param_match = re.search(r'[?&]id=([a-zA-Z0-9-_]+)', url)
    if id_param_match:
        file_id = id_param_match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
        
    return None

def fetch_google_sheet_csv(url: str) -> Optional[str]:
    """
    Скачивает содержимое таблицы Google Sheets в формате CSV.
    """
    csv_url = convert_google_sheet_url_to_csv(url)
    if not csv_url:
        return None
        
    try:
        response = requests.get(csv_url, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Failed to fetch Google Sheet CSV from {csv_url}: {e}")
        return None

async def parse_chessboard_table(csv_content: str) -> List[Dict[str, Any]]:
    """
    Парсит таблицу шахматки (CSV) с помощью Pandas и Gemini, 
    преобразуя каждую строку объекта в структуру Units Airtable.
    """
    if not csv_content or len(csv_content.strip()) < 10:
        return []
        
    try:
        df = pd.read_csv(io.StringIO(csv_content), on_bad_lines='skip')
        df = df.dropna(how='all')
        
        table_snippet = df.head(50).to_string()
        
        if not client:
            logger.warning("Gemini client not initialized for chessboard parsing")
            return []
            
        prompt = f"""
Ты — аналитик шахматок (availability charts) недвижимости.
Перед тобой фрагмент таблицы шахматки объекта недвижимости (CSV/Dataframe):

```
{table_snippet}
```

Твоя задача — извлечь список юнитов (квартир/вилл) и привести каждый к структуре:
- Unit type: "Villa" | "Apartment" | "Loft" | "Studio" | "Townhouse"
- Area from (m2): площадь в м2 (число)
- Price from (USD): цена в USD (число)
- Bedrooms: количество спален (число)
- Availability: "On sale" | "Sold" | "Blocked"
- Stage: стадия
- Pool: "Yes(Private)" | "Yes(Shared)" | "No"

Верни JSON-массив извлеченных юнитов.
"""
        model_name = resolve_model_name()
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        
        data = json.loads(response.text)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "Units" in data:
            return data["Units"]
        return []
    except Exception as e:
        logger.error(f"Error parsing chessboard table: {e}")
        return []

async def process_google_link(url: str) -> Dict[str, Any]:
    """
    Единая точка входа для обработки ссылок Google Drive / Google Sheets.
    """
    result = {
        "source_url": url,
        "type": "unknown",
        "units": [],
        "text_content": ""
    }
    
    if "spreadsheets" in url:
        result["type"] = "google_sheets_chessboard"
        csv_data = fetch_google_sheet_csv(url)
        if csv_data:
            units = await parse_chessboard_table(csv_data)
            result["units"] = units
            result["text_content"] = f"Извлечено юнитов из шахматки: {len(units)}"
    elif "drive.google.com" in url:
        result["type"] = "google_drive_files"
        direct_url = convert_google_drive_url_to_direct_download(url)
        result["direct_download_url"] = direct_url
        result["text_content"] = f"Ссылка на Google Drive скачивание: {direct_url}"
        
    return result
