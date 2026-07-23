import os
import logging
import difflib
from pyairtable import Api
from datetime import datetime

logger = logging.getLogger("AirtableClient")

AIRTABLE_TOKEN = os.environ.get('AIRTABLE_TOKEN')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID')

api = Api(AIRTABLE_TOKEN) if AIRTABLE_TOKEN else None

def get_base():
    if not api or not AIRTABLE_BASE_ID:
        return None
    return api.base(AIRTABLE_BASE_ID)

def fuzzy_match_developer(name: str, existing_records: list):
    """Ищет разработчика по имени с использованием difflib."""
    if not name or not existing_records:
        return None, 0.0

    names = [r['fields'].get('Developer', '') for r in existing_records if r.get('fields', {}).get('Developer')]
    
    if not names:
        return None, 0.0

    matches = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
    
    if matches:
        best_match = matches[0]
        # Вычисляем точный score
        score = difflib.SequenceMatcher(None, name.lower(), best_match.lower()).ratio()
        
        # Находим запись с этим именем
        for r in existing_records:
            if r['fields'].get('Developer') == best_match:
                return r, score
                
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

    # Ищем по названию (точное совпадение)
    # pyairtable использует формулы Airtable для поиска
    formula = f"{{Project Name}} = '{proj_name}'"
    existing = table.all(formula=formula)

    fields = {k: v for k, v in proj_data.items() if v}
    if dev_id:
        fields['Developer'] = [dev_id]
        
    fields['Status'] = "Draft"
    fields['Source'] = "TG: Rise Real Bali Chat"
    fields['Last updated'] = datetime.now().isoformat()
    if gaps:
        fields['Gaps'] = ", ".join(gaps)
    else:
        fields['Gaps'] = "" # Очищаем gaps если их нет

    if existing:
        rec_id = existing[0]['id']
        logger.info(f"Updating project '{proj_name}' (ID: {rec_id})")
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
