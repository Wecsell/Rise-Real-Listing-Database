import os
import json
import logging
import re
from google import genai
from google.genai import types

logger = logging.getLogger("GeminiParser")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Полный системный промпт, соответствующий всем колонкам Airtable базы Base RR New
SYSTEM_PROMPT = """
Ты — эксперт-аналитик по недвижимости на Бали в компании Rise Real.
Твоя задача — извлечь максимум информации из сообщения или описания застройщика для полного заполнения колонок базы данных Airtable.

Схема полей, которые ты должен найти и затянуть:

1. ДАННЫЕ ПРОЕКТА (Projects):
- project_name: Название ЖК / проекта
- developer_name: Название компании застройщика
- location_area: Район (Canggu, Pererenan, Uluwatu, Ubud, Seminyak, Seseh, Nuanu, Kedungu, Umalas и т.д.)
- property_type: Тип недвижимости (Villa, Apartment, Loft, Studio, Townhouse, Hotel)
- construction_stage: Стадия строительства (Off-plan / Pre-sales, Foundation, Structure, Finishing, Completed)
- completion_date: Дата сдачи / Handover (месяц и год, или Q1 2026)
- ownership_type: Форма владения (Leasehold, Freehold)
- leasehold_years: Срок лизхолда в годах (например: 25, 30, 50)
- yield_roi_percent: Прогнозируемый ROI / доходность в %
- price_from_usd: Минимальная цена проекта "от" в USD
- offers_discount: Текст персональных скидок, рассрочек или горячих акций
- coordinates: Ссылки или точные координаты на Google Maps

2. ДАННЫЕ ЮНИТА (Units):
- unit_id: Номер юнита (например: Villa 3, Apt 204)
- unit_type: Тип конкретного юнита (Villa, Apartment, Loft, Penthouse)
- bedrooms: Количество спален (число)
- bathrooms: Количество ванных комнат (число)
- area_sqm: Площадь юнита в м²
- price_usd: Точная цена юнита в USD
- availability: Статус доступности (On sale, Blocked, Sold)
- view_type: Вид (Ocean, Jungle, Rice Fields, Garden, City)
- amenities: Удобства (Pool, Rooftop, Garden, Smart Home, Turnkey, Furnished, Parking)

3. ВНЕШНИЕ ССЫЛКИ:
- detected_urls: Все ссылки на Google Drive, Google Sheets, Notion, Dropbox, PDF-презентации и рендеры.

Верни строго JSON по следующей схеме:
{
  "is_relevant": true/false,
  "project": {
    "project_name": "текст или null",
    "developer_name": "текст или null",
    "location_area": "текст или null",
    "property_type": "текст или null",
    "construction_stage": "текст или null",
    "completion_date": "текст или null",
    "ownership_type": "текст или null",
    "leasehold_years": число или null,
    "yield_roi_percent": число или null,
    "price_from_usd": число или null,
    "offers_discount": "текст или null",
    "coordinates": "текст или null"
  },
  "unit": {
    "unit_id": "текст или null",
    "unit_type": "текст или null",
    "bedrooms": число или null,
    "bathrooms": число или null,
    "area_sqm": число или null,
    "price_usd": число или null,
    "availability": "On sale / Blocked / Sold или null",
    "view_type": "текст или null",
    "amenities": ["список удобств"]
  },
  "detected_urls": ["список всех найденных ссылок"],
  "confidence": число от 0.0 до 1.0,
  "reason": "краткое описание найденного"
}
"""

async def parse_message(text: str) -> dict:
    if not client:
        logger.warning("GEMINI_API_KEY is not configured.")
        return {"is_relevant": False, "reason": "No GEMINI_API_KEY set"}

    if not text or len(text.strip()) < 3:
        return {"is_relevant": False, "reason": "Message too short"}

    try:
        response = await client.aio.models.generate_content(
            model='gemini-2.0-flash',
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        
        text_resp = response.text.strip()
        if text_resp.startswith("```"):
            text_resp = re.sub(r"^```(?:json)?\n?|```$", "", text_resp).strip()
            
        return json.loads(text_resp)
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return {"is_relevant": False, "error": str(e)}
