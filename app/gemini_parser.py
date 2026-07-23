import os
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger("GeminiParser")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

SYSTEM_PROMPT = """
Ты — ассистент компании Rise Real Bali, эксперт по недвижимости на Бали.
Твоя задача — проанализировать сообщение из чата застройщика и извлечь структурированные данные.

Правила:
1. Проверь, относится ли сообщение к недвижимости (цены, шахматки, рендеры, доступность юнитов, скидки).
   Если это бытовой разговор, приветствие, картинка без текста или флуд — установи is_relevant: false.
2. Найди названия проектов/ЖК (например: Magnum, Solan, Rait, Tangi).
3. Найди информацию о ценах (приведи к USD), юнитах, спальнях, районах или внешних ссылках (Google Drive, Notion, Sheets).
4. Заполни JSON строго по заданной схеме:
{
  "is_relevant": true/false,
  "project_name": "Название проекта или null",
  "unit_type": "Villa / Apartment / Loft / Studio / Townhouse или null",
  "bedrooms": число или null,
  "price_usd": число или null,
  "price_from": true/false,
  "offer_text": "текст скидки или null",
  "detected_urls": ["список ссылок на файлы/диски"],
  "confidence": число от 0.0 до 1.0,
  "reason": "краткое пояснение"
}
"""

async def parse_message(text: str) -> dict:
    if not client:
        logger.warning("GEMINI_API_KEY is not configured. Skipping Gemini LLM extraction.")
        return {"is_relevant": False, "reason": "No GEMINI_API_KEY set"}

    if not text or len(text.strip()) < 3:
        return {"is_relevant": False, "reason": "Message too short"}

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return {"is_relevant": False, "error": str(e)}
