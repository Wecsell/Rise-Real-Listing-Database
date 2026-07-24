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

VALID_PROJECT_AREAS = ['Kuta', 'Seminyak', 'Canggu', 'Kerobokan', 'Umalas', 'Pererenan', 'Seseh', 'Cemagi', 'Nuanu', 'Kedungu', 'Jimbaran', 'Nusa Dua', 'Ungasan', 'Uluwatu', 'Sanur', 'Ubud', 'Karengasem', 'Karangasem', 'Sumba', 'Bukit', 'Mengwi', 'Buduk', 'Melasti', 'Kutuh', 'Pecatu', 'Berawa', 'Lombok', 'Badung', 'South Kuta', 'Bingin']
VALID_UNIT_AREAS = VALID_PROJECT_AREAS

def sanitize_area(raw_area, valid_areas):
    if not raw_area:
        return None
    raw_lower = str(raw_area).lower()
    for area in valid_areas:
        if area.lower() in raw_lower:
            return area
    return raw_area  # Fallback to original, which might fail validation but that's caught by try-except


def get_base():
    if not api or not AIRTABLE_BASE_ID:
        return None
    return api.base(AIRTABLE_BASE_ID)

def get_projects_by_developer(chat_or_dev_name: str = None) -> list[str]:
    base = get_base()
    if not base:
        return []
    try:
        table = base.table('Projects')
        records = table.all()
        
        if not chat_or_dev_name:
            names = [r['fields'].get('Project Name') for r in records if r.get('fields', {}).get('Project Name')]
            return list(set(names))
            
        dev_table = base.table('Developer')
        dev_records = dev_table.all()
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

def fuzzy_match_project(name: str, existing_records: list):
    """Ищет проект по имени с использованием difflib и поиска подстрок."""
    if not name or not existing_records:
        return None, 0.0

    name_clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(name)).lower().strip()
    name_words = set(name_clean.split())
    ignore_words = {'villas', 'resort', 'project', 'complex', 'phase', '1', '2', '3', 'очередь', 'фаза'}

    best_record = None
    best_score = 0.0

    for r in existing_records:
        p_name = r['fields'].get('Project Name')
        if not p_name:
            continue
        p_clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(p_name)).lower().strip()
        p_words = set(p_clean.split())
        
        if p_clean in name_clean or name_clean in p_clean:
            score = 0.85
            if score > best_score:
                best_score = score
                best_record = r

        meaningful_p = p_words - ignore_words
        if meaningful_p and meaningful_p.issubset(name_words):
            score = 0.8
            if score > best_score:
                best_score = score
                best_record = r

        diff_score = difflib.SequenceMatcher(None, p_clean, name_clean).ratio()
        if diff_score > best_score:
            best_score = diff_score
            best_record = r

    if best_score >= 0.55:
        return best_record, best_score

    return None, 0.0

def fuzzy_match_developer(name: str, existing_records: list):
    """Ищет разработчика по имени с использованием поиска подстрок и пересечения слов."""
    if not name or not existing_records:
        return None, 0.0

    name_clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(name)).lower().strip()
    name_words = set(name_clean.split())
    ignore_words = {'official', 'chat', 'bali', 'real', 'estate', 'channel', 'group', 'news', 'bot', 'villas'}

    best_record = None
    best_score = 0.0

    for r in existing_records:
        dev_name = r['fields'].get('Developer')
        if not dev_name:
            continue
            
        dev_clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(dev_name)).lower().strip()
        dev_words = set(dev_clean.split())
        
        if dev_clean in name_clean or name_clean in dev_clean:
            score = 0.85
            if score > best_score:
                best_score = score
                best_record = r

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

    if best_score >= 0.5:
        return best_record, best_score

    return None, 0.0

async def upsert_developer(dev_data: dict) -> str:
    """Создает или обновляет Developer. Возвращает Record ID."""
    base = get_base()
    if not base:
        return None

    table = base.table('Developer')
    existing = table.all()
    
    dev_name = dev_data.get('Developer')
    if not dev_name:
        return None

    match, score = fuzzy_match_developer(dev_name, existing)
    
    # Готовим поля
    fields = {k: v for k, v in dev_data.items() if v and k != "Projects"}
    fields['Listed By'] = "Mikhail"
    fields['Listed By'] = "Mikhail"

    if match:
        rec_id = match['id']
        if score < 1.0:
            # Неточное совпадение, можно добавить пометку в Notes или оставить как есть
            logger.info(f"Fuzzy matched developer '{dev_name}' to '{match['fields'].get('Developer')}' (Score: {score:.2f})")
            
        # Обновляем (upsert)
        table.update(rec_id, fields)
        return rec_id
    else:
        # Создаем
        logger.info(f"Creating new developer '{dev_name}'")
        record = table.create(fields)
        return record['id']

async def upsert_project(proj_data: dict, dev_id: str, gaps: list) -> str:
    """Создает или обновляет Project. Возвращает Record ID."""
    base = get_base()
    if not base:
        return None

    table = base.table('Projects')
    proj_name = proj_data.get('Project Name')
    if not proj_name:
        return None

    existing = table.all()
    match, score = fuzzy_match_project(proj_name, existing)

    fields = {k: v for k, v in proj_data.items() if v}
    if dev_id:
        fields['Developer'] = [dev_id]
        
    # Map field names from JSON schema to Airtable schema
    if 'Link to Dev Kit (Rus)' in fields:
        fields["Link to Developer’s Kit (Rus)"] = fields.pop('Link to Dev Kit (Rus)')
    if 'Link to Dev Kit (Eng)' in fields:
        fields["Link to Developer’s Kit (Eng)"] = fields.pop('Link to Dev Kit (Eng)')

    if 'Район' in fields:
        fields['Район'] = sanitize_area(fields['Район'], VALID_PROJECT_AREAS)

    if 'Downpayment' in fields:
        try:
            val = float(fields['Downpayment'])
            if val > 1.0:
                fields['Downpayment'] = val / 100.0
        except (ValueError, TypeError):
            pass

    fields['Status'] = "Draft"
    fields['Source'] = "TG: Rise Real Bali Chat"
    fields['Last updated'] = datetime.now().isoformat()
    if gaps:
        fields['Gaps'] = ", ".join(gaps)
    else:
        fields['Gaps'] = "" # Очищаем gaps если их нет

    if match:
        rec_id = match['id']
        logger.info(f"Updating project '{proj_name}' matched to '{match['fields'].get('Project Name')}' (ID: {rec_id}, Score: {score:.2f})")
        table.update(rec_id, fields)
        return rec_id
    else:
        logger.info(f"Creating project '{proj_name}'")
        record = table.create(fields)
        return record['id']

async def upsert_unit(unit_data: dict, proj_id: str, proj_name: str, gaps: list) -> str:
    """Создает или обновляет Unit."""
    base = get_base()
    if not base:
        return None

    table = base.table('Units')
    
    # Генерация ключа для юнита. Формат: project-slug__type__bed__price
    u_type = str(unit_data.get('Unit type', 'none')).lower()
    beds = str(unit_data.get('Bedrooms', '0'))
    price = str(unit_data.get('Price from (USD)', '0'))
    proj_slug = re.sub(r'[^a-z0-9]', '', str(proj_name).lower())[:10]
    
    key = f"{proj_slug}__{u_type}__{beds}br__{price}"
    
    formula = f"{{Key}} = '{key}'"
    existing = table.all(formula=formula)

    fields = {k: v for k, v in unit_data.items() if v}
    if proj_id:
        fields['Project Name'] = [proj_id]
        
    # Map field names from JSON schema to Airtable schema
    if 'Price from (USD)' in fields:
        fields['Price from(USD)'] = fields.pop('Price from (USD)')
    if 'Area from (m2)' in fields:
        fields['Area from (m\xb2)'] = fields.pop('Area from (m2)')
    if 'Land Area (m2)' in fields:
        fields['Land Area (m\xb2)'] = fields.pop('Land Area (m2)')

    if 'Area' in fields:
        fields['Area'] = sanitize_area(fields['Area'], VALID_UNIT_AREAS)

    fields['Key'] = key
    fields['Status'] = "Draft"
    fields['Source'] = "TG: Rise Real Bali Chat"
    fields['Last updated'] = datetime.now().isoformat()
    if gaps:
        fields['Gaps'] = ", ".join(gaps)
    else:
        fields['Gaps'] = ""

    if existing:
        rec_id = existing[0]['id']
        logger.info(f"Updating unit '{key}' (ID: {rec_id})")
        table.update(rec_id, fields)
        return rec_id
    else:
        logger.info(f"Creating unit '{key}'")
        record = table.create(fields)
        return record['id']
