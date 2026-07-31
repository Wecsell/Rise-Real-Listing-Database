import os
import json
import asyncio
import logging
import re
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional

from app import content_cache

logger = logging.getLogger("GeminiParser")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

class DeveloperData(BaseModel):
    Developer: Optional[str] = None
    Contacts: Optional[str] = None
    Language: Optional[str] = None
    Country_of_developer: Optional[str] = Field(default=None, alias="Country of developer")
    Notes: Optional[str] = None

class ProjectData(BaseModel):
    Project_Name: Optional[str] = Field(default=None, alias="Project Name")
    District: Optional[str] = None
    # Точная локация (Berawa, Bingin, Pecatu) — не теряется при сведении к району
    Location: Optional[str] = None
    Location_Link: Optional[str] = Field(default=None, alias="Location Link")
    Coordinates_for_Map: Optional[str] = Field(default=None, alias="Coordinates(for Map)")
    Developer_Link: Optional[str] = Field(default=None, alias="Developer Link")
    Property_Type: Optional[List[str]] = Field(default=None, alias="Property Type")
    Total_Units: Optional[int] = Field(default=None, alias="Total Units")
    Price_From_USD: Optional[float] = Field(default=None, alias="Price From (USD)")
    Price_To_USD: Optional[float] = Field(default=None, alias="Price To (USD)")
    Construction_stage: Optional[str] = Field(default=None, alias="Construction stage")
    Distance_to_beach: Optional[int] = Field(default=None, alias="Distance to beach")
    View: Optional[List[str]] = None
    Property_Management: Optional[str] = Field(default=None, alias="Property Management")
    Handover_Date: Optional[str] = Field(default=None, alias="Handover Date")
    Downpayment: Optional[float] = None
    Installment_Notes: Optional[str] = Field(default=None, alias="Installment Notes")
    Ownership_Type: Optional[str] = Field(default=None, alias="Ownership Type")
    Lease_Term_years: Optional[float] = Field(default=None, alias="Lease Term (years)")
    Extension_Term_years: Optional[str] = Field(default=None, alias="Extension Term (years)")
    Renewal_Right: Optional[str] = Field(default=None, alias="Renewal Right")
    Land_Zoning_Color: Optional[str] = Field(default=None, alias="Land Zoning Color")
    Handover_Permits: Optional[str] = Field(default=None, alias="Handover Permits")
    Special_Conditions: Optional[str] = Field(default=None, alias="Special Conditions")
    Link_to_Dev_Kit_Rus: Optional[str] = Field(default=None, alias="Link to Dev Kit (Rus)")
    Link_to_Dev_Kit_Eng: Optional[str] = Field(default=None, alias="Link to Dev Kit (Eng)")
    Availability_Chart: Optional[str] = Field(default=None, alias="Availability Chart")
    Priority: Optional[str] = None

class UnitData(BaseModel):
    Unit_type: Optional[str] = Field(default=None, alias="Unit type")
    Area: Optional[str] = None
    View: Optional[List[str]] = None
    Total_Floors: Optional[int] = Field(default=None, alias="Total Floors")
    Area_from_m2: Optional[float] = Field(default=None, alias="Area from (m2)")
    Land_Area_m2: Optional[float] = Field(default=None, alias="Land Area (m2)")
    Price_from_USD: Optional[float] = Field(default=None, alias="Price from(USD)")
    Bedrooms: Optional[int] = None
    Bathrooms: Optional[int] = None
    Bathtub: Optional[str] = None
    Terrace_Balcony: Optional[str] = Field(default=None, alias="Terrace/Balcony")
    Rooftop: Optional[str] = None
    Pool: Optional[str] = None
    Parking: Optional[str] = None
    Gym: Optional[str] = None
    Spa: Optional[str] = None
    Restaurant: Optional[str] = None
    leasehold_years: Optional[float] = Field(default=None, alias="leasehold years")
    Freehold: Optional[str] = None
    Stage: Optional[str] = None
    Availability: Optional[str] = None
    Plan_Link: Optional[str] = Field(default=None, alias="Plan Link")

class ParsedExtraction(BaseModel):
    is_relevant: bool
    Developer: Optional[DeveloperData] = None
    Projects: Optional[ProjectData] = None
    Units: Optional[List[UnitData]] = None
    Gaps: Optional[List[str]] = None
    detected_urls: Optional[List[str]] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None

SYSTEM_PROMPT = """
Ты — эксперт-аналитик по недвижимости на Бали в компании Rise Real.
Твоя задача — извлечь максимум информации из любого входящего текстового сообщения, файла (PDF/брошюра/шахматка), фото или аудиокомментария застройщика/агента для заполнения колонок базы данных Airtable.

1. Developer (Застройщик):
- Developer: Точное имя застройщика. Если в тексте прямо не указано имя девелопера, используй название чата/канала из КОНТЕКСТА.
- Contacts: Telegram-канал, телефон или контакты отдела продаж.
- Language: "English", "Ru" или "Id".
- Country of developer: Страна происхождения девелопера (если упоминается).
- Notes: Важные заметки или прямая транскрипция аудио от агента.

2. Projects (Проект / Комплекс):
- Project Name: Название проекта (ОБЯЗАТЕЛЬНО). Если 2 фаза/очередь, сохраняй с указанием фазы.
- District: СТРОГО одно из (закрытый список значений Airtable, других база не примет):
  "Uluwatu", "Ungasan", "Nusa Dua", "Jimbaran", "Bukit", "Kuta", "Seminyak", "Canggu",
  "Seseh", "Cemagi", "Nuanu", "Kedungu", "Tabanan", "Sanur", "Denpasar", "Ubud",
  "Karangasem", "Amed", "Lovina", "Munduk", "Kintamani", "Bedugul",
  "Lombok", "Sumba", "Nusa Penida".
  Мелкие локации своди к ближайшему району из списка: Berawa/Batu Bolong/Echo Beach/Buduk -> "Canggu";
  Bingin/Pecatu/Padang Padang -> "Uluwatu"; Melasti/Kutuh -> "Ungasan"; Mengwi/Tanah Lot -> "Tabanan";
  Penestanan -> "Ubud"; Sidemen -> "Karangasem".
  Если понятно только что объект на южном полуострове, но не ясен конкретный район — ставь "Bukit".
- Location: точное название местности как в источнике (например "Berawa", "Bingin", "Pecatu"). Заполняй всегда, когда оно известно, даже если District сведен к более крупному.
- Location Link: Ссылка на Google Maps.
- Coordinates(for Map): СТРОГО "долгота, широта" — сначала число около 115, потом отрицательное около -8.
  Это обратный порядок относительно Google Maps: в ссылке после @ идет "широта,долгота" (сначала -8..., потом 115...),
  их надо ПОМЕНЯТЬ МЕСТАМИ. Пример: ссылка .../@-8.638668,115.106234 -> записать "115.106234, -8.638668".
  Карта Airtable читает именно такой порядок, при обратном точки встают не туда.
- Location Link: ссылка на Google Maps в обычном порядке "широта,долгота" — местами НЕ менять.
- Property Type: Массив строк. СТРОГО только эти значения: ["Villa"], ["Apartment"], ["Studio"], ["Townhouse"].
- Price From (USD) / Price To (USD): Диапазон цен в USD (число без валютных знаков).
- Construction stage: СТРОГО один из: "Off-plan / Pre-sales", "Foundation", "Structure", "Finishing", "Completed".
- Distance to beach: Дистанция до пляжа в метрах (число).
- View: Массив строк из: ["Ocean", "Jungle", "Rice Fields", "Garden / Courtyard", "Mountains", "Water Features", "City / Neighborhood", "No View"].
- Property Management: Название управляющей компании или условия (например, "70/30", "Fixed ROI 10%", "Self-management").
- Handover Date: Дата сдачи проекта СТРОГО в формате YYYY-MM-DD (например: Q1 2027 -> 2027-03-31, Q2 2027 -> 2027-06-30, Q3 2027 -> 2027-09-30, Q4 2027 / Late 2027 -> 2027-12-31, Mid 2026 -> 2026-06-30).
- Downpayment: Первоначальный взнос в процентах (число, например 30 для 30%).
- Installment Notes: Описание условия рассрочки (например, "0% на 12 месяцев", "Рассрочка до конца строительства").
- Ownership Type: СТРОГО один из: "Leasehold", "Freehold", "Hak Pakai".
- Lease Term (years): Срок аренды в годах (число, например 25 или 30).
- Extension Term (years): Условия продления (например "25 years", "30 years fixed").
- Renewal Right: СТРОГО один из: "Guaranteed at Market Price", "Fixed Price", "Priority at Market Price", "Prepaid".
- Land Zoning Color: СТРОГО один из: "Residential", "Tourism/Mixed", "Brown", "Green".
- Handover Permits: СТРОГО один из: "PBG in process", "PBG", "PBG/SLF in process", "PBG/SLF".
- Priority: СТРОГО один из: "Высокий", "Средний", "Низкий". ("Высокий" — только при личной похвале агентом в аудио или пометке срочно; "Низкий" — при негативных отзывах или юридических рисках "зеленая зона/без PBG").

3. Units (Юниты / Лоты):
- Unit type: СТРОГО один из: "Villa", "Apartment", "Loft", "Studio", "Townhouse", "Penthouse" (БЕЗ спален в названии!).
- Area: Район Бали на английском (например, Ubud, Canggu).
- Area from (m2): Общая/жилая площадь юнита в м² (число).
- Land Area (m2): Площадь участка (для вилл) в м² (число).
- Price from(USD): Цена юнита в USD (число).
- Bedrooms: Количество спален (число, например 1, 2, 3).
- Bathrooms: Количество ванных комнат (число).
- Pool: СТРОГО один из: "Yes(Private)", "Yes(Shared)", "No".
- Availability: СТРОГО один из: "On sale", "Blocked", "Sold".

ОБЩИЕ ПРАВИЛА:
- Заполняй только те поля, где есть фактические данные. Не придумывай неверные числа.
- Помещай имена всех пропущенных или не найденных полей в массив `Gaps` на уровне JSON.
- Указывай итоговый коэффициент уверенности `confidence` от 0.0 до 1.0. Если данные фрагментарны или противоречивы, ставь confidence < 0.7.
"""

DEFAULT_MODEL = 'gemini-2.5-flash'

# Порядок предпочтения при авто-детекте. Без него бралась первая попавшаяся
# модель из выдачи API, то есть качество разбора зависело от порядка ответа.
PREFERRED_MODELS = ('gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-1.5-flash')

_cached_model_name = None


def resolve_model_name() -> str:
    """
    Возвращает имя рабочей модели, выполняя авто-детект один раз.

    Единая точка получения модели для всех парсеров. Важно вызывать функцию,
    а не импортировать _cached_model_name: при импорте по значению вызывающий
    модуль навсегда получал None и молча уходил в фолбэк.
    """
    global _cached_model_name
    if _cached_model_name:
        return _cached_model_name

    if not client:
        _cached_model_name = DEFAULT_MODEL
        return _cached_model_name

    try:
        available = [m.name for m in client.models.list() if 'flash' in m.name.lower()]
        chosen = None
        for preferred in PREFERRED_MODELS:
            for name in available:
                if preferred in name:
                    chosen = name
                    break
            if chosen:
                break

        if not chosen and available:
            chosen = available[0]
            logger.warning(f"Ни одна из предпочитаемых моделей не найдена, берем: {chosen}")

        _cached_model_name = chosen or DEFAULT_MODEL
        logger.info(f"Выбрана модель Gemini: {_cached_model_name}")
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        _cached_model_name = DEFAULT_MODEL

    return _cached_model_name


async def parse_message(text: str, chat_title: str = None) -> dict:
    if not client:
        logger.warning("GEMINI_API_KEY is not configured.")
        return {"is_relevant": False, "reason": "No GEMINI_API_KEY set"}

    if not text or len(text.strip()) < 3:
        return {"is_relevant": False, "reason": "Message too short"}

    # Один и тот же текст может прийти дважды: пересылка в чате, повторный
    # скан истории при перезапуске listener. Проверяем кэш до похода в
    # Airtable за списком проектов застройщика — попадание экономит и его.
    cache_key = content_cache.hash_text(text, context=chat_title or '')
    cached = await asyncio.to_thread(content_cache.get, cache_key)
    if cached is not None:
        logger.info(f"💾 Cache hit for message (chat='{chat_title}'), skipping Gemini call")
        return cached
    logger.info(f"Cache miss (chat='{chat_title}'), calling Gemini...")

    model_name = resolve_model_name()

    try:
        from app.airtable_client import get_projects_by_developer
        existing_projects = get_projects_by_developer(chat_title)
        
        dynamic_prompt = SYSTEM_PROMPT
        if chat_title:
            dynamic_prompt += f"\n\nКОНТЕКСТ: Сообщение получено из Telegram-чата/канала: '{chat_title}'."

        if existing_projects:
            proj_str = ", ".join([f'"{p}"' for p in existing_projects if p])
            dynamic_prompt += f"\n\nВНИМАНИЕ! У этого застройщика в нашей базе УЖЕ есть следующие проекты: [{proj_str}].\nСТРОГОЕ ПРАВИЛО: Если в тексте прямо или косвенно идет речь об одном из них (упомянут тот же район/локация, фразы '2 очередь', 'новая фаза', или опечатка), ты ОБЯЗАН вернуть ТОЧНОЕ название проекта из списка выше. Не придумывай новые названия!"
            
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=dynamic_prompt,
                response_mime_type="application/json",
                response_schema=ParsedExtraction,
                temperature=0.1
            )
        )
        
        text_resp = response.text.strip()
        parsed_json = json.loads(text_resp)

        if content_cache.is_cacheable(parsed_json):
            await asyncio.to_thread(content_cache.put, cache_key, 'message', parsed_json)

        return parsed_json
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return {"is_relevant": False, "error": str(e)}
