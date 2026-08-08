import os
import json
import asyncio
import logging
import re
import hashlib
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional

from app import content_cache
from app import llm_gate

logger = logging.getLogger("GeminiParser")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Клиент не создаётся, пока разбор выключен (llm_gate): наличие ключа в
# окружении не должно само по себе включать обращения к API.
client = genai.Client(api_key=GEMINI_API_KEY) if llm_gate.llm_enabled() else None

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
    # Projects."Developer Link" здесь НЕ объявляется: это lookup имени
    # застройщика через связь Developer, то есть дубль того, что и так видно в
    # поле Developer. Пока поле было в схеме ответа, модель имела право его
    # вернуть, а запись в lookup роняет ВЕСЬ проект с 422 (убрано 06.08.2026).
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
- Developer: Точное имя застройщика. Если в тексте прямо не указано имя девелопера, используй название чата/канала из КОНТЕКСТА
  как ПОСЛЕДНЮЮ меру — только когда никакого другого способа определить компанию нет.
  Название чата/партнёрства/группы НИКОГДА не становится Project Name — только Developer, и то временно.
  Живой пример ошибки (08.08.2026): чат "Rise Real & Avo Estate" породил проект с ИМЕНЕМ ЧАТА вместо
  реального объекта (в шахматке по ссылке первая ячейка называлась "Rise Villas" — уже существующий
  проект); чат "ADVA X RISE REAL" — партнёрство/группа, а не застройщик, реальный застройщик ADVA
  назван внутри самой таблицы. Прежде чем брать имя из чата — проверь первую строку/ячейку файла
  по ссылке в сообщении: настоящее имя почти всегда там.
- Contacts: СПОСОБ СВЯЗИ, а не имя. Одних имён ("Тим, Герман") недостаточно — по ним нельзя написать.
  Собирай ссылки и номера: wa.me/<цифры>, @telegram_username, телефон, email отдела продаж.
  Номер приводи к ссылке WhatsApp через phone_formatter.format_single_phone (0813... -> 62813...).
  Имя оставляй только рядом с контактом: "Tim: https://wa.me/6282144353695".
  Кнопки "WhatsApp" на сайте застройщика — это ссылки wa.me, номер зашит в href, доставай оттуда.
- Language: "English", "Ru" или "Id".
- Country of developer: Страна происхождения девелопера (если упоминается).
- Notes: Важные заметки или прямая транскрипция аудио от агента.

2. Projects (Проект / Комплекс):
- Project Name: Название проекта (ОБЯЗАТЕЛЬНО). Если 2 фаза/очередь, сохраняй с указанием фазы.
  НИКОГДА не бери за имя проекта название Telegram-чата, канала или партнёрства/группы застройщиков
  ("ADVA X RISE REAL", "Rise Real & Avo Estate") — это источник, а не объект. Если по ссылке из
  сообщения открывается таблица/файл — реальное имя почти всегда стоит в его первой строке/ячейке
  или в названии листа, а не в тексте сообщения.
- ОДНА таблица/шахматка может описывать НЕСКОЛЬКО РАЗНЫХ проектов, разложенных по колонкам
  (например строка "Project" в шапке с несколькими значениями подряд: "Adva Village | Six Stars |
  Serenity | Terracotta"). Это не один проект с "типологиями" из разных колонок — это отдельные
  проекты, каждый со своим Project Name, своими юнитами, своей ценой и площадью. Разбирай такую
  таблицу по колонкам, не сливай их в одну карточку и не переставляй значения между колонками
  (спальни одного проекта не подставляй под цену другого).
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
- Coordinates(for Map): СТРОГО "долгота, широта" — сначала число около 115, потом отрицательное около -8.
  Это обратный порядок относительно Google Maps: в ссылке после @ идет "широта,долгота" (сначала -8..., потом 115...),
  их надо ПОМЕНЯТЬ МЕСТАМИ. Пример: ссылка .../@-8.638668,115.106234 -> записать "115.106234, -8.638668".
  Карта Airtable читает именно такой порядок, при обратном точки встают не туда.
  Короткая ссылка (maps.app.goo.gl/..., goo.gl/maps/..., bit.ly/...) координат НЕ содержит: из нее
  ничего не выводи и не угадывай. Ставь поле в Gaps — координаты достанет человек или инструмент,
  раскрыв редирект до полного вида .../@-8.638668,115.106234.
- Location Link: ссылка на Google Maps в обычном порядке "широта,долгота" — местами НЕ менять.
- Property Type: Массив строк. СТРОГО только эти значения: ["Villa"], ["Apartment"], ["Studio"], ["Townhouse"].
- Price From (USD) / Price To (USD): Диапазон цен в USD (число без валютных знаков).
- Construction stage: СТРОГО один из: "Off-plan / Pre-sales", "Foundation", "Structure", "Finishing", "Completed".
- Distance to beach: Дистанция до пляжа в метрах (число).
  Если в источнике только время пешком — пересчитай по средней скорости пешехода 5 км/ч
  (83 м в минуту) и округли до сотен: "5 минут пешком"/"5 minutes walk" -> 400.
  Пересчитывай ТОЛЬКО время пешком. Время на машине/байке ("10 минут до Чангу") в метры не переводи —
  это про дорогу, а не про расстояние; такие фразы оставляй в Gaps.
- View: Массив строк из: ["Ocean", "Jungle", "Rice Fields", "Garden / Courtyard", "Mountains", "Water Features", "City / Neighborhood", "No View"].
- Property Management: Название управляющей компании или условия (например, "70/30", "Fixed ROI 10%", "Self-management").
- Handover Date: Дата сдачи проекта СТРОГО в формате YYYY-MM-DD (например: Q1 2027 -> 2027-03-31, Q2 2027 -> 2027-06-30, Q3 2027 -> 2027-09-30, Q4 2027 / Late 2027 -> 2027-12-31, Mid 2026 -> 2026-06-30).
- Downpayment: Первоначальный взнос в процентах (число, например 30 для 30%).
- Installment Notes: Описание условия рассрочки (например, "0% на 12 месяцев", "Рассрочка до конца строительства").
- Ownership Type: СТРОГО один из: "Leasehold", "Freehold", "Hak Pakai".
- Lease Term (years): Срок аренды в годах (число, например 25 или 30).
- Extension Term (years): Условия продления (например "25 years", "30 years fixed").
- Renewal Right: СТРОГО один из: "Guaranteed at Market Price", "Fixed Price", "Priority at Market Price", "Prepaid".
- Land Zoning Color: СТРОГО один из: "Residential", "Tourism/Mixed", "Brown", "Green", "Red/Commercial".
  В документах застройщика зона называется цветом на карте RTRW, а не значением Airtable. Перевод:
  розовый/pink, "tourist designation", "pariwisata" -> "Tourism/Mixed"; желтый/yellow, "perumahan" -> "Residential";
  зеленый/green, "сельхоз", "green belt" -> "Green"; красный/red, "commercial" -> "Red/Commercial".
  Цвет бери только если он назван в источнике. По престижности или туристичности района НЕ выводи:
  "проект в туристической зоне Нуса-Дуа" — это не основание ставить "Tourism/Mixed".
  (Этот список сверяется с живой базой в app.schema_check и очищается перед записью в
  app.airtable_client.sanitize_land_zoning - если он опять устареет, запись не упадёт
  молча, но лучше держать его синхронным.)
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
- Stage: НЕ ЗАПОЛНЯЙ. Это lookup из Projects."Construction stage" — стадия подтягивается из проекта
  автоматически. Попытка записи упадёт с 422, как и в любое вычисляемое поле.
- Area: НЕ ЗАПОЛНЯЙ. Тоже lookup (район из связанного проекта), а не обычный select.
- Freehold: СТРОГО "yes" или "not" — со строчной буквы и именно "not", а не "no".
- Terrace/Balcony: СТРОГО "Yes" или "Not" (не "No"). У виллы с садом терраса есть — ставь "Yes".
- Pool, Gym, Restaurant, Spa, Co-working: инфраструктура комплекса относится и к юниту.
  "фитнес и йога-зона" -> Gym; "ресторан и лаунж" -> Restaurant; "приватный бассейн" -> Pool "Yes(Private)".
- Цену, площадь и наличие НЕ бери из рекламного поста в чате: их источник — только шахматка
  (см. rules.md, иерархия источников). Пост обычно описывает спецпредложение на один лот, а запись
  в базе описывает тип юнита целиком.

ПРИОРИТЕТ ИСТОЧНИКОВ (проект и юниты, при расхождении между материалами одного застройщика):
- Шахматка (Availability Chart) старше презентации, брошюры и финансовой модели по ВСЕМ полям, КРОМЕ
  даты сдачи. Листы финмодели с ценой в названии ("2BR_450k", "2BR_500k") — это СЦЕНАРИИ доходности
  под разную цену входа, а не прайс-лист; не путай название листа с фактической ценой продажи.
- Дата сдачи — единственное исключение: если источники расходятся, бери САМУЮ ПОЗДНЮЮ дату из всех
  (SUMMARY, шахматка, презентация, сайт застройщика). Стройка сдвигается почти всегда, ускоряется
  почти никогда — поздняя дата ближе к реальности независимо от того, в каком документе она стоит.

ОБЩИЕ ПРАВИЛА:
- Заполняй только те поля, где есть фактические данные. Не придумывай неверные числа.
- Поле "Active" НЕ ЗАПОЛНЯЙ никогда, ни в проекте, ни в юните. Это отметка о ручной проверке
  человеком и признак видимости записи в интерфейсе; её ставит только человек в Airtable.
  Значение, пришедшее от модели, всё равно будет отброшено (airtable_client.HUMAN_ONLY_FIELDS).
- Поле "Status" тоже не заполняй: оно считается из списка пропусков автоматически.
- Поле "Source" не заполняй: его ставит код по каналу, через который материал попал к нам
  ("TG: <чат>", "WA: <чат>", "Manual"). Адрес самого материала (Notion, сайт застройщика) источником
  НЕ является — ссылка на него всё равно пришла из чата или от владельца.
- Данные соседнего проекта того же застройщика переноси, ТОЛЬКО если источник прямо это утверждает
  ("участок и локация те же, что у Y-WAY", "due diligence общий"). Тогда переносятся поля участка:
  Land Zoning Color, Ownership Type, Handover Permits, координаты. Сроки, цены и стадию строительства
  не переноси никогда — они у проектов на одном участке разные.
- Помещай имена всех пропущенных или не найденных полей в массив `Gaps` на уровне JSON.
- Указывай итоговый коэффициент уверенности `confidence` от 0.0 до 1.0. Если данные фрагментарны или противоречивы, ставь confidence < 0.7.
"""

# T1 по implementation_plan.md ("Выбор моделей по уровням"): сухое чтение
# сообщений чата - высокий объём, решение "релевантно/нет". Берётся lite:
# втрое быстрее при той же цене за токен, а её осторожность здесь не мешает.
# Разбор документов (T2) идёт другой моделью - field_extractor.DOC_MODEL.
DEFAULT_MODEL = 'gemini-3.5-flash-lite'

# Порядок предпочтения при авто-детекте. Без него бралась первая попавшаяся
# модель из выдачи API, то есть качество разбора зависело от порядка ответа.
PREFERRED_MODELS = ('gemini-3.5-flash-lite', 'gemini-3.5-flash',
                    'gemini-2.5-flash-lite', 'gemini-2.5-flash')

_cached_model_name = None

# Changing either the extraction contract or the model may change the answer
# for identical source text.  Keep the cache namespace explicit as an extra
# guard for changes outside the prompt/schema payload below.
MESSAGE_CACHE_VERSION = "2"


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

        # Сравниваем по точному имени модели, а не подстрокой: 'gemini-3.5-flash'
        # является подстрокой 'gemini-3.5-flash-lite', и наивное `preferred in name`
        # молча возвращало бы не ту модель в зависимости от порядка выдачи API
        # (в живой выдаче полная flash идёт РАНЬШЕ lite). Тот же класс бага, что
        # 'are' внутри 'share' в предфильтре listener.py.
        by_bare_name = {name.split('/')[-1]: name for name in available}

        chosen = None
        for preferred in PREFERRED_MODELS:
            if preferred in by_bare_name:
                chosen = by_bare_name[preferred]
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


def _message_cache_context(
    chat_title: Optional[str], model_name: str, existing_projects: Optional[List[str]]
) -> str:
    """Build a stable cache context for every input that can alter parsing.

    ``parse_message`` augments the system prompt with projects already known
    for a developer.  Reusing a result produced before that list, the model,
    or the response schema changed can silently assign a message to the wrong
    project.  The fingerprint makes those variants intentionally distinct.
    """
    known_projects = sorted(
        str(project).strip()
        for project in (existing_projects or [])
        if project and str(project).strip()
    )
    contract = {
        "cache_version": MESSAGE_CACHE_VERSION,
        "chat_title": chat_title or "",
        "existing_projects": known_projects,
        "model": model_name,
        "response_schema": ParsedExtraction.model_json_schema(),
        "system_prompt": SYSTEM_PROMPT,
    }
    serialized = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def parse_message(text: str, chat_title: str = None) -> dict:
    if not client:
        # is_relevant=False здесь НЕ ставится: это утверждение «в сообщении нет
        # данных», которое мы не проверяли. Разбор не выполнялся — так и пишем,
        # иначе сообщение застройщика тихо теряется (см. app/llm_gate.py).
        result = llm_gate.manual_required(f"parse_message({(text or '')[:40]!r})")
        result["is_relevant"] = None
        return result

    if not text or len(text.strip()) < 3:
        return {"is_relevant": False, "reason": "Message too short"}

    model_name = resolve_model_name()

    try:
        from app.airtable_client import get_projects_by_developer
        existing_projects = get_projects_by_developer(chat_title)

        # The cache key includes every value that changes the model request,
        # including the developer's current project list.  Correctness wins
        # over a stale cache hit when another worker has just created a project.
        cache_context = _message_cache_context(chat_title, model_name, existing_projects)
        cache_key = content_cache.hash_text(text, context=cache_context)
        cached = await asyncio.to_thread(content_cache.get, cache_key)
        if cached is not None:
            logger.info(f"💾 Cache hit for message (chat='{chat_title}'), skipping Gemini call")
            return cached
        logger.info(f"Cache miss (chat='{chat_title}'), calling Gemini...")
        
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
