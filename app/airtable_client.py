import os
import re
import logging
import difflib
from pyairtable import Api
from datetime import datetime

logger = logging.getLogger("AirtableClient")

AIRTABLE_TOKEN = os.environ.get('AIRTABLE_TOKEN')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID')

api = Api(AIRTABLE_TOKEN) if AIRTABLE_TOKEN else None

# Запасной список на случай, если схему не удалось прочитать из Airtable.
# Боевой источник истины — сама база, см. get_select_options().
FALLBACK_PROJECT_AREAS = ['Kuta', 'Seminyak', 'Canggu', 'Kerobokan', 'Umalas', 'Pererenan', 'Seseh', 'Cemagi', 'Nuanu', 'Kedungu', 'Jimbaran', 'Nusa Dua', 'Ungasan', 'Uluwatu', 'Sanur', 'Ubud', 'Karengasem', 'Sumba']
VALID_UNIT_AREAS = ['Ubud', 'Cemagi', 'Kuta', 'Sumba', 'Canggu', 'Bukit', 'Mengwi', 'Nuanu', 'Ungasan', 'Buduk', 'Seseh', 'Melasti', 'Sanur', 'Kutuh', 'Pecatu', 'Uluwatu', 'Nusa Dua', 'Berawa', 'Lombok', 'Karangasem', 'Badung', 'South Kuta', 'Bingin']

_SCHEMA_OPTIONS = None
_SCHEMA_FIELDS = {}


def _load_schema_options() -> dict:
    """
    Читает списки значений всех singleSelect/multipleSelect прямо из Airtable.

    Захардкоженные списки неизбежно расходятся с базой: так уже случилось с
    'Karangasem' против 'Karengasem' в коде и базе, с Priority (русские значения
    против Hight/Medium/Low) и с Property Type. Каждое расхождение приводит к
    молчаливой потере поля или к падению записи целиком.
    """
    import json
    import urllib.request

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        return {}

    url = f'https://api.airtable.com/v0/meta/bases/{AIRTABLE_BASE_ID}/tables'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {AIRTABLE_TOKEN}'})
    result = {}
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)

    for table in data.get('tables', []):
        _SCHEMA_FIELDS[table['name']] = {f['name'] for f in table.get('fields', [])}
        for field in table.get('fields', []):
            choices = field.get('options', {}).get('choices')
            if choices:
                result[(table['name'], field['name'])] = [c['name'] for c in choices]
    return result


def field_exists(table: str, field: str) -> bool:
    """
    Есть ли такое поле в таблице. Запись в несуществующее поле роняет весь
    апдейт записи с 422, а не игнорируется — поэтому новые поля, которых может
    не быть в базе, пишем только после проверки.
    """
    get_select_options(table, field)  # гарантирует, что схема загружена
    known = _SCHEMA_FIELDS.get(table)
    if not known:
        return False
    return field in known


def get_select_options(table: str, field: str, fallback=None) -> list:
    """Актуальные значения селекта из базы. При сбое — запасной список."""
    global _SCHEMA_OPTIONS
    if _SCHEMA_OPTIONS is None:
        try:
            _SCHEMA_OPTIONS = _load_schema_options()
            logger.info(f"Схема Airtable прочитана: {len(_SCHEMA_OPTIONS)} селектов")
        except Exception as e:
            logger.error(f"Не удалось прочитать схему Airtable, работаем по запасным спискам: {e}")
            _SCHEMA_OPTIONS = {}
    return _SCHEMA_OPTIONS.get((table, field)) or (fallback or [])


def get_valid_project_areas() -> list:
    return get_select_options('Projects', 'District', FALLBACK_PROJECT_AREAS)

CACHE_DEVELOPERS = []
CACHE_PROJECTS = []
CACHE_UNITS = []
CACHE_INITIALIZED = False

import asyncio

def init_cache():
    """Синхронная версия для обратной совместимости (блокирующая)"""
    global CACHE_DEVELOPERS, CACHE_PROJECTS, CACHE_UNITS, CACHE_INITIALIZED
    base = get_base()
    if not base:
        return
    logger.info("Initializing Airtable cache (minimal fields)...")
    
    CACHE_DEVELOPERS = base.table('Developer').all(fields=['Developer'])
    CACHE_PROJECTS = base.table('Projects').all(fields=['Project Name', 'Developer', 'District'])
    CACHE_UNITS = base.table('Units').all(fields=['Key'])
    
    CACHE_INITIALIZED = True
    logger.info(f"Cache initialized: {len(CACHE_DEVELOPERS)} devs, {len(CACHE_PROJECTS)} projects, {len(CACHE_UNITS)} units.")

async def init_cache_async():
    """Асинхронная инициализация в отдельном потоке (не блокирует event loop)"""
    global CACHE_INITIALIZED
    if CACHE_INITIALIZED:
        return
    await asyncio.to_thread(init_cache)

# Каждое значение справа обязано существовать в селекте 'District' Airtable.
# Раньше добрая половина алиасов вела на несуществующие значения (Berawa, Bingin,
# Bukit, Pecatu, Mengwi...), из-за чего район молча выбрасывался: 33 проекта из
# 47 остались без района.
AREA_ALIASES = {
    # --- Букит (южный полуостров) ---
    'улувату': 'Uluwatu', 'uluwatu': 'Uluwatu',
    'бинжин': 'Uluwatu', 'бингин': 'Uluwatu', 'bingin': 'Uluwatu',
    'пекату': 'Uluwatu', 'pecatu': 'Uluwatu',
    'паданг паданг': 'Uluwatu', 'padang padang': 'Uluwatu',
    'унгасан': 'Ungasan', 'ungasan': 'Ungasan',
    'кутух': 'Ungasan', 'kutuh': 'Ungasan',
    'мелести': 'Ungasan', 'melasti': 'Ungasan',
    'джимбаран': 'Jimbaran', 'jimbaran': 'Jimbaran',
    'нуса дуа': 'Nusa Dua', 'нуса-дуа': 'Nusa Dua', 'nusa dua': 'Nusa Dua',
    'букит': 'Bukit', 'bukit': 'Bukit',  # только если конкретнее не опознали

    # --- Юг и запад ---
    'кута': 'Kuta', 'kuta': 'Kuta', 'south kuta': 'Kuta',
    'семиньяк': 'Seminyak', 'семиньяке': 'Seminyak', 'seminyak': 'Seminyak',
    'чангу': 'Canggu', 'чанггу': 'Canggu', 'canggu': 'Canggu',
    'берава': 'Canggu', 'berawa': 'Canggu',
    'будук': 'Canggu', 'buduk': 'Canggu',
    'бату болонг': 'Canggu', 'batu bolong': 'Canggu',
    'эхо бич': 'Canggu', 'echo beach': 'Canggu',
    'сесех': 'Seseh', 'seseh': 'Seseh',
    'чемаги': 'Cemagi', 'cemagi': 'Cemagi',
    'нуану': 'Nuanu', 'nuanu': 'Nuanu',
    'кедунгу': 'Kedungu', 'kedungu': 'Kedungu',
    'табанан': 'Tabanan', 'tabanan': 'Tabanan',
    'менгви': 'Tabanan', 'mengwi': 'Tabanan',
    'танах лот': 'Tabanan', 'tanah lot': 'Tabanan',

    # --- Центр и восток ---
    'санур': 'Sanur', 'sanur': 'Sanur',
    'денпасар': 'Denpasar', 'denpasar': 'Denpasar',
    'убуд': 'Ubud', 'ubud': 'Ubud',
    'пенестанан': 'Ubud', 'penestanan': 'Ubud',
    'карангасем': 'Karangasem', 'karangasem': 'Karangasem',
    'сидемен': 'Karangasem', 'sidemen': 'Karangasem',
    'амед': 'Amed', 'amed': 'Amed',

    # --- Север и горы ---
    'ловина': 'Lovina', 'lovina': 'Lovina',
    'мундук': 'Munduk', 'munduk': 'Munduk',
    'кинтамани': 'Kintamani', 'kintamani': 'Kintamani',
    'бедугул': 'Bedugul', 'bedugul': 'Bedugul',

    # --- Острова ---
    'ломбок': 'Lombok', 'lombok': 'Lombok',
    'сумба': 'Sumba', 'sumba': 'Sumba',
    'нуса пенида': 'Nusa Penida', 'nusa penida': 'Nusa Penida',

    # --- Оставлены по решению: в основной список не входят, но районы отдельные ---
    'керобокан': 'Kerobokan', 'kerobokan': 'Kerobokan',
    'умалас': 'Umalas', 'umalas': 'Umalas',
    'перерена': 'Pererenan', 'перереран': 'Pererenan',
    'перереан': 'Pererenan', 'pererenan': 'Pererenan',
}

# Локации Букита: если конкретный район не опознан, ставим 'Bukit' с пометкой
# на ручную проверку, а не теряем район совсем.
BUKIT_HINTS = ('букит', 'bukit', 'south kuta', 'badung selatan')

# Значения селектов, которые код умеет отдавать. Держим на уровне модуля,
# чтобы schema_check сверял их с живой базой, а не со второй копией списка.
VALID_STAGES = {"Off-plan / Pre-sales", "Foundation", "Structure", "Finishing", "Completed"}
VALID_POOL_VALUES = {"No", "Yes(Private)", "Yes(Shared)"}

def sanitize_area(raw_area, valid_areas, is_project=False):
    if not raw_area:
        return None
    raw_str = str(raw_area).strip()
    raw_lower = raw_str.lower()
    
    # Melasti -> Ungasan для проектов (RULES.md)
    if is_project and 'melasti' in raw_lower:
        return 'Ungasan'
    
    # 1. Точное совпадение с валидным списком
    for area in valid_areas:
        if area.lower() == raw_lower:
            return area
    
    # 2. Проверяем алиасы (русский, опечатки)
    # Разбиваем на части по запятой (случай "Penestanan, Ubud")
    parts = [p.strip().lower() for p in raw_lower.split(',')]
    for part in parts:
        if part in AREA_ALIASES:
            candidate = AREA_ALIASES[part]
            # Убеждаемся, что candidate есть в valid_areas
            if candidate in valid_areas:
                return candidate
    
    # 3. Подстрока (последний шанс)
    for area in valid_areas:
        if area.lower() in raw_lower:
            return area

    # 4. Алиас по подстроке: "вилла в Берава" -> Canggu
    for alias, canonical in AREA_ALIASES.items():
        if alias in raw_lower and canonical in valid_areas:
            return canonical

    # 5. Букит: конкретный район не опознали, но понятно, что это южный
    # полуостров. Лучше поставить 'Bukit' с пометкой на ручную правку,
    # чем потерять район совсем.
    if is_project and 'Bukit' in valid_areas and any(h in raw_lower for h in BUKIT_HINTS):
        logger.info(f"Район '{raw_area}' сведен к 'Bukit' — требуется ручное уточнение")
        return 'Bukit'

    logger.warning(f"Area '{raw_area}' is invalid and was cleared to prevent Airtable 422 error.")
    return None

UNIT_TYPE_ALIASES = {
    'mini villa': 'Villa',
    'villa 1br': 'Villa', '1br villa': 'Villa',
    'villa 2br': 'Villa', '2br villa': 'Villa',
    'villa 3br': 'Villa', '3br villa': 'Villa',
    'villa 4br': 'Villa', '4br villa': 'Villa',
    'villa 1br jungle view': 'Villa', '1br jungle view villa': 'Villa',
    'residence': 'Villa',
    'commercial': None,  # Удалять — мы не храним коммерцию
    'penthouse': 'Apartment',
    'bungalow': 'Villa',
}

def sanitize_unit_type(raw_type):
    """Нормализует Unit type до одного из валидных: Villa, Apartment, Loft, Studio, Townhouse, Hotel, Hotel room"""
    if not raw_type:
        return None
    raw_lower = str(raw_type).strip().lower()
    
    # Точные валидные значения
    valid = {'villa': 'Villa', 'apartment': 'Apartment', 'loft': 'Loft', 
             'studio': 'Studio', 'townhouse': 'Townhouse', 'hotel': 'Hotel', 'hotel room': 'Hotel room'}
    if raw_lower in valid:
        return valid[raw_lower]
    
    # Алиасы
    if raw_lower in UNIT_TYPE_ALIASES:
        return UNIT_TYPE_ALIASES[raw_lower]
    
    # Содержит ключевое слово
    if 'villa' in raw_lower:
        return 'Villa'
    if 'apartment' in raw_lower:
        return 'Apartment'
    if 'loft' in raw_lower:
        return 'Loft'
    if 'studio' in raw_lower:
        return 'Studio'
    if 'townhouse' in raw_lower:
        return 'Townhouse'
    
    logger.warning(f"Unit type '{raw_type}' is unknown, stripping it.")
    return None

def sanitize_pool(raw_pool):
    """Нормализует Pool до одного из валидных: Yes(Private), Yes(Shared), No"""
    if not raw_pool:
        return None
    raw_lower = str(raw_pool).strip().lower()
    
    if raw_lower == 'no':
        return 'No'
    if raw_lower in ('yes(private)', 'yes (private)', 'private'):
        return 'Yes(Private)'
    if raw_lower in ('yes(shared)', 'yes (shared)', 'shared'):
        return 'Yes(Shared)'
    if raw_lower == 'yes':
        return 'Yes(Private)'  # По умолчанию: если написано "yes" — скорее всего private pool
    
    logger.warning(f"Pool value '{raw_pool}' is unknown, stripping it.")
    return None

def format_drive_link(url: str) -> str:
    """Конвертирует ссылки Google Drive в каноничный формат Airtable Attachment"""
    if not url or not isinstance(url, str): return url
    match = re.search(r'id=([a-zA-Z0-9_-]+)', url) or re.search(r'/d/([a-zA-Z0-9_-]+)/', url)
    if match:
        return f"https://drive.google.com/thumbnail?id={match.group(1)}&sz=w2000"
    return url

def safe_float(val):
    if val is None:
        return None
    try:
        # Убираем пробелы и запятые (частая проблема, когда ИИ пишет "120,000" вместо 120000)
        clean_val = str(val).replace(',', '').replace(' ', '').replace('$', '')
        return float(clean_val)
    except (ValueError, TypeError):
        return val

import requests

def robust_airtable_op(func, *args, fields=None, **kwargs):
    """Синхронная обертка для безопасного выполнения (устарела, используйте async)"""
    try:
        if 'fields' in kwargs:
            kwargs['fields'] = {k: v for k, v in kwargs['fields'].items() if v is not None and str(v).strip() != ""}
        elif fields:
            kwargs['fields'] = {k: v for k, v in fields.items() if v is not None and str(v).strip() != ""}
        
        return func(*args, **kwargs)
    except requests.exceptions.HTTPError as e:
        logger.error(f"Airtable HTTP Error 422: {e}")
        return {'id': None, 'fields': {}}
    except Exception as e:
        logger.error(f"Unexpected Airtable Error: {e}")
        return {'id': None, 'fields': {}}

async def robust_airtable_op_async(func, *args, fields=None, **kwargs):
    """Асинхронная обертка, запускающая HTTP-запросы в отдельном потоке."""
    return await asyncio.to_thread(robust_airtable_op, func, *args, fields=fields, **kwargs)

def get_base():
    if not api or not AIRTABLE_BASE_ID:
        return None
    return api.base(AIRTABLE_BASE_ID)

def get_projects_by_developer(chat_or_dev_name: str = None) -> list[str]:
    """Синхронная версия поиска проектов"""
    if not CACHE_INITIALIZED:
        init_cache()
        
    try:
        records = CACHE_PROJECTS
        
        if not chat_or_dev_name:
            names = [r['fields'].get('Project Name') for r in records if r.get('fields', {}).get('Project Name')]
            return list(set(names))
            
        dev_records = CACHE_DEVELOPERS
        matched_dev, score = fuzzy_match_developer(chat_or_dev_name, dev_records)
        
        if matched_dev and score >= 0.4:
            dev_id = matched_dev['id']
            proj_names = []
            for r in records:
                devs = r['fields'].get('Developer', [])
                if dev_id in devs and r['fields'].get('Project Name'):
                    proj_names.append(r['fields'].get('Project Name'))
            if proj_names:
                return list(set(proj_names))

        names = [r['fields'].get('Project Name') for r in records if r.get('fields', {}).get('Project Name')]
        return list(set(names))
    except Exception as e:
        logger.error(f"Error fetching projects by developer: {e}")
        return []

def fuzzy_match_project(name: str, existing_records: list, area: str = None, dev_id: str = None):
    """Ищет проект по имени с использованием difflib и поиска подстрок. Учитывает иерархию застройщиков."""
    if not name or str(name).strip().lower() == 'none' or not existing_records:
        return None, 0.0

    name_clean = re.sub(r'[^\w\s]', '', str(name)).lower().strip()
    name_words = set(name_clean.split())
    ignore_words = {'villas', 'resort', 'project', 'complex', 'phase', '1', '2', '3', 'очередь', 'фаза'}

    best_record = None
    best_score = 0.0

    for r in existing_records:
        p_name = r['fields'].get('Project Name')
        if not p_name:
            continue
            
        r_devs = r['fields'].get('Developer')
        if r_devs and dev_id:
            # Если у проекта уже есть застройщик, и он не совпадает с текущим dev_id, пропускаем! (Строгая иерархия)
            if dev_id not in r_devs:
                continue
                
        p_area = r['fields'].get('District')
        
        # Если передан район, и в базе есть район, и они разные - штрафуем совпадение
        area_mismatch = False
        if area and p_area:
            if str(area).lower() != str(p_area).lower():
                area_mismatch = True

        p_clean = re.sub(r'[^\w\s]', '', str(p_name)).lower().strip()
        p_words = set(p_clean.split())
        
        score = 0.0
        
        # Убрано слабое сравнение через 'in'

        meaningful_p = p_words - ignore_words
        if meaningful_p and meaningful_p.issubset(name_words):
            if score < 0.8:
                score = 0.8

        diff_score = difflib.SequenceMatcher(None, p_clean, name_clean).ratio()
        if diff_score > score:
            score = diff_score
            
        # Если район не совпадает - штраф
        if area_mismatch:
            score -= 0.3

        if score > best_score:
            best_score = score
            best_record = r

    if best_score >= 0.90:
        return best_record, best_score

    return None, 0.0

def fuzzy_match_developer(name: str, existing_records: list):
    """Ищет разработчика по имени с использованием поиска подстрок и пересечения слов."""
    if not name or str(name).strip().lower() == 'none' or not existing_records:
        return None, 0.0

    name_clean = re.sub(r'[^\w\s]', '', str(name)).lower().strip()
    name_words = set(name_clean.split())
    ignore_words = {'official', 'chat', 'bali', 'real', 'estate', 'channel', 'group', 'news', 'bot', 'villas'}

    best_record = None
    best_score = 0.0

    for r in existing_records:
        dev_name = r['fields'].get('Developer')
        if not dev_name:
            continue
            
        dev_clean = re.sub(r'[^\w\s]', '', str(dev_name)).lower().strip()
        dev_words = set(dev_clean.split())
        
        # Убрано слабое сравнение через 'in'
        
        meaningful_dev_words = dev_words - ignore_words
        if meaningful_dev_words and meaningful_dev_words.issubset(name_words):
            score = 0.85
            if score > best_score:
                best_score = score
                best_record = r

        diff_score = difflib.SequenceMatcher(None, dev_clean, name_clean).ratio()
        if diff_score > best_score:
            best_score = diff_score
            best_record = r

    if best_score >= 0.90:
        return best_record, best_score

    return None, 0.0

async def upsert_developer(dev_data: dict) -> str:
    """Создает или обновляет Developer. Возвращает Record ID."""
    global CACHE_DEVELOPERS
    
    if not CACHE_INITIALIZED:
        await init_cache_async()
        
    base = get_base()
    if not base:
        return None

    table = base.table('Developer')
    existing = CACHE_DEVELOPERS
    
    dev_name = dev_data.get('Developer')
    if not dev_name:
        return None

    match, score = fuzzy_match_developer(dev_name, existing)
    
    # Готовим поля
    fields = {k: v for k, v in dev_data.items() if v and k != "Projects"}
    fields['Listed By'] = "Mikhail"
    if 'Contacts' in fields and isinstance(fields['Contacts'], list):
        fields['Contacts'] = ", ".join([str(x) for x in fields['Contacts'] if x])

    if match:
        rec_id = match['id']
        if score < 1.0:
            # Неточное совпадение, можно добавить пометку в Notes или оставить как есть
            logger.info(f"Fuzzy matched developer '{dev_name}' to '{match['fields'].get('Developer')}' (Score: {score:.2f})")
            
        # Обновляем (upsert)
        record = await robust_airtable_op_async(table.update, rec_id, fields=fields)
        if not record or not record.get('id'):
            logger.warning(f"Failed to update Developer {rec_id} (likely deleted in Airtable). Removing from cache and creating fresh record...")
            CACHE_DEVELOPERS = [c for c in CACHE_DEVELOPERS if c['id'] != rec_id]
            record = await robust_airtable_op_async(table.create, fields=fields)
            if record and record.get('id'):
                CACHE_DEVELOPERS.append(record)
                return record['id']
            return None
        
        # Обновляем кэш
        for i, c in enumerate(CACHE_DEVELOPERS):
            if c['id'] == rec_id:
                CACHE_DEVELOPERS[i] = record
                break
                
        return rec_id
    else:
        # Создаем
        logger.info(f"Creating new developer '{dev_name}'")
        record = await robust_airtable_op_async(table.create, fields=fields)
        if record and record.get('id'):
            CACHE_DEVELOPERS.append(record)
            return record['id']
        return None

async def upsert_project(proj_data: dict, dev_id: str, gaps: list) -> str:
    """Создает или обновляет Project. Возвращает Record ID."""
    global CACHE_PROJECTS
    
    if not CACHE_INITIALIZED:
        await init_cache_async()
        
    base = get_base()
    if not base:
        return None

    table = base.table('Projects')
    proj_name = proj_data.get('Project Name')
    if not proj_name:
        return None

    # Сохраняем все значения, кроме None и пустых строк
    fields = {k: v for k, v in proj_data.items() if v is not None and str(v).strip() != ""}
    
    # Таблица Projects не содержит поля Notes, удаляем его во избежание ошибки 422
    if 'Notes' in fields:
        fields.pop('Notes')
        
    # Значения вынесены в VALID_STAGES на уровне модуля — их использует schema_check
    if 'Construction stage' in fields:
        if fields['Construction stage'] not in VALID_STAGES:
            stage_str = str(fields['Construction stage']).lower()
            if 'under' in stage_str or 'construction' in stage_str or 'building' in stage_str:
                fields['Construction stage'] = "Structure"
            elif 'off' in stage_str or 'plan' in stage_str or 'pre' in stage_str:
                fields['Construction stage'] = "Off-plan / Pre-sales"
            elif 'finish' in stage_str:
                fields['Construction stage'] = "Finishing"
            elif 'complete' in stage_str or 'done' in stage_str or 'ready' in stage_str:
                fields['Construction stage'] = "Completed"
            else:
                fields.pop('Construction stage')
    
    # 0 в этих полях означает отсутствие данных от Gemini
    zero_is_meaningless = {'Price From (USD)', 'Price To (USD)', 'Total Units', 'Lease Term (years)', 
                           'Extension Term (years)', 'Distance to beach'}
    for field_name in zero_is_meaningless:
        if field_name in fields and (fields[field_name] == 0 or fields[field_name] == "0"):
            fields.pop(field_name)
            
    if dev_id:
        fields['Developer'] = [dev_id]
        
    if 'District' in fields:
        raw_area = fields['District']
        s_area = sanitize_area(raw_area, get_valid_project_areas(), is_project=True)
        if s_area:
            fields['District'] = s_area
            # Точное название сохраняем отдельно: сведение Berawa -> Canggu
            # не должно стирать исходную локацию. Поле может быть еще не заведено
            # в базе, поэтому пишем только если оно там есть.
            if (str(raw_area).strip().lower() != s_area.lower()
                    and field_exists('Projects', 'Location')):
                fields.setdefault('Location', str(raw_area).strip())
            if s_area == 'Bukit':
                gaps = list(gaps or [])
                gaps.append(f"Район уточнить вручную (распознан только Букит, исходно: {raw_area})")
        else:
            # Раньше район выбрасывался молча — так потерялись 33 проекта из 47.
            fields.pop('District')
            gaps = list(gaps or [])
            gaps.append(f"Район не распознан: {raw_area}")
        
    existing = CACHE_PROJECTS
    match, score = fuzzy_match_project(proj_name, existing, area=fields.get('District'), dev_id=dev_id)

    # Map field names from JSON schema to Airtable schema
    if 'Link to Dev Kit (Rus)' in fields:
        fields["Link to Developer’s Kit (Rus)"] = fields.pop('Link to Dev Kit (Rus)')
    if 'Link to Dev Kit (Eng)' in fields:
        fields["Link to Developer’s Kit (Eng)"] = fields.pop('Link to Dev Kit (Eng)')
    if 'Distance to beach' in fields:
        fields["Distance to the beach, m2"] = fields.pop('Distance to beach')

    if 'Img' in fields:
        img_data = fields['Img']
        if isinstance(img_data, str):
            fields['Img'] = [{"url": format_drive_link(img_data)}]
        elif isinstance(img_data, list):
            formatted_imgs = []
            for item in img_data:
                if isinstance(item, str):
                    formatted_imgs.append({"url": format_drive_link(item)})
                elif isinstance(item, dict) and 'url' in item:
                    item['url'] = format_drive_link(item['url'])
                    formatted_imgs.append(item)
            fields['Img'] = formatted_imgs

    numeric_fields = ['Price From (USD)', 'Price To (USD)', 'Total Units', 'Lease Term (years)', 'Extension Term (years)']
    for f in numeric_fields:
        if f in fields:
            fields[f] = safe_float(fields[f])

    if 'Downpayment' in fields:
        try:
            val = float(fields['Downpayment'])
            if val > 1.0:
                fields['Downpayment'] = val / 100.0
            else:
                fields['Downpayment'] = val
        except (ValueError, TypeError):
            pass

    fields['Status'] = "Needs data" if gaps else "Verified"
    fields['Source'] = "TG: Rise Real Bali Chat"
    fields['Last updated'] = datetime.now().isoformat()
    if gaps:
        fields['Gaps'] = ", ".join(gaps)
    else:
        fields['Gaps'] = "" # Очищаем gaps если их нет

    if match:
        rec_id = match['id']
        logger.info(f"Updating project '{proj_name}' matched to '{match['fields'].get('Project Name')}' (ID: {rec_id}, Score: {score:.2f})")
        record = await robust_airtable_op_async(table.update, rec_id, fields=fields)
        if not record or not record.get('id'):
            logger.warning(f"Failed to update Project {rec_id} (likely deleted in Airtable). Removing from cache and creating fresh record...")
            CACHE_PROJECTS = [c for c in CACHE_PROJECTS if c['id'] != rec_id]
            record = await robust_airtable_op_async(table.create, fields=fields)
            if record and record.get('id'):
                CACHE_PROJECTS.append(record)
                return record['id']
            return None
        
        # Обновляем кэш
        for i, c in enumerate(CACHE_PROJECTS):
            if c['id'] == rec_id:
                CACHE_PROJECTS[i] = record
                break
                
        return rec_id
    else:
        logger.info(f"Creating project '{proj_name}'")
        record = await robust_airtable_op_async(table.create, fields=fields)
        if not record or not record.get('id'):
            logger.error(f"Failed to create Project {proj_name}")
            return None
        CACHE_PROJECTS.append(record)
        return record['id']

async def upsert_unit(unit_data: dict, proj_id: str, proj_name: str, gaps: list) -> str:
    """Создает или обновляет Unit."""
    global CACHE_UNITS
    
    if not CACHE_INITIALIZED:
        await init_cache_async()
        
    base = get_base()
    if not base:
        return None

    table = base.table('Units')
    
    # Нормализуем Unit type ДО генерации ключа, чтобы ключи были стабильными
    raw_unit_type = unit_data.get('Unit type')
    clean_unit_type = sanitize_unit_type(raw_unit_type)
    
    # Генерация ключа для юнита. Канон: project__unitno__Nbr при наличии номера; иначе project__type__Nbr__views БЕЗ цены
    u_type = str(clean_unit_type or 'none').lower()
    beds = str(unit_data.get('Bedrooms', '0'))
    unit_no = unit_data.get('Unit Number') 
    
    view_raw = unit_data.get('View', '')
    if isinstance(view_raw, list):
        view = '-'.join(str(v).lower() for v in view_raw)
    else:
        view = str(view_raw).lower()
    
    proj_slug = re.sub(r'[^a-z0-9-]', '', str(proj_name).lower().replace(' ', '-'))[:15]
    
    if unit_no:
        key = f"{proj_slug}__{str(unit_no).lower()}__{beds}br"
    else:
        view_slug = re.sub(r'[^a-z0-9]+', '-', view).strip('-')
        if view_slug and view_slug != 'none':
            key = f"{proj_slug}__{u_type}__{beds}br__{view_slug}"
        else:
            key = f"{proj_slug}__{u_type}__{beds}br"
    
    existing = [u for u in CACHE_UNITS if u.get('fields', {}).get('Key') == key]

    # Сохраняем все значения, кроме None и пустых строк
    fields = {k: v for k, v in unit_data.items() if v is not None and str(v).strip() != ""}
    if 'Notes' in fields:
        fields.pop('Notes')
    # Убираем нулевые плейсхолдеры Gemini
    unit_zero_meaningless = {'Price from (USD)', 'Bedrooms', 'Bathrooms', 'Total Floors', 
                             'Area from (m2)', 'Land Area (m2)', 'leasehold years'}
    for field_name in unit_zero_meaningless:
        if field_name in fields and (fields[field_name] == 0 or fields[field_name] == "0"):
            fields.pop(field_name)
    if proj_id:
        fields['Project Name'] = [proj_id]
        
    # Sanitize Unit type
    if clean_unit_type:
        fields['Unit type'] = clean_unit_type
    else:
        fields.pop('Unit type', None)
    
    # Sanitize Pool
    if 'Pool' in fields:
        clean_pool = sanitize_pool(fields['Pool'])
        if clean_pool:
            fields['Pool'] = clean_pool
        else:
            fields.pop('Pool')
        
    # Map field names from JSON schema to Airtable schema
    if 'Price from (USD)' in fields:
        fields['Price from(USD)'] = fields.pop('Price from (USD)')
    if 'Area from (m2)' in fields:
        fields['Area from (m\xb2)'] = fields.pop('Area from (m2)')
    if 'Land Area (m2)' in fields:
        fields['Land Area (m\xb2)'] = fields.pop('Land Area (m2)')

    if 'Img' in fields:
        img_data = fields['Img']
        if isinstance(img_data, str):
            fields['Img'] = [{"url": format_drive_link(img_data)}]
        elif isinstance(img_data, list):
            formatted_imgs = []
            for item in img_data:
                if isinstance(item, str):
                    formatted_imgs.append({"url": format_drive_link(item)})
                elif isinstance(item, dict) and 'url' in item:
                    item['url'] = format_drive_link(item['url'])
                    formatted_imgs.append(item)
            fields['Img'] = formatted_imgs

    if 'Area' in fields:
        s_area = sanitize_area(fields['Area'], VALID_UNIT_AREAS)
        if s_area:
            fields['Area'] = s_area
        else:
            fields.pop('Area')
        
    numeric_fields = ['Price from(USD)', 'Bedrooms', 'Bathrooms', 'Total Floors', 'Area from (m\xb2)', 'Land Area (m\xb2)', 'leasehold years']
    for f in numeric_fields:
        if f in fields:
            fields[f] = safe_float(fields[f])

    fields['Key'] = key
    fields['Status'] = "Needs data" if gaps else "Verified"
    fields['Source'] = "TG: Rise Real Bali Chat"
    fields['Last updated'] = datetime.now().isoformat()
    if gaps:
        fields['Gaps'] = ", ".join(gaps)
    else:
        fields['Gaps'] = ""

    if existing:
        rec_id = existing[0]['id']
        logger.info(f"Updating unit '{key}' (ID: {rec_id})")
        record = await robust_airtable_op_async(table.update, rec_id, fields=fields)
        if not record or not record.get('id'):
            logger.error(f"Failed to update Unit {key}")
            return rec_id
        
        # Обновляем кэш
        for i, c in enumerate(CACHE_UNITS):
            if c['id'] == rec_id:
                CACHE_UNITS[i] = record
                break
                
        return rec_id
    else:
        logger.info(f"Creating unit '{key}'")
        record = await robust_airtable_op_async(table.create, fields=fields)
        if not record or not record.get('id'):
            logger.error(f"Failed to create Unit {key}")
            return None
        CACHE_UNITS.append(record)
        return record['id']
