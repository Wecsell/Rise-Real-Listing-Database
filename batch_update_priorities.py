import os
from pyairtable import Api
from dotenv import load_dotenv
from app.priority_parser import parse_priority

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

records = table.all()
print(f"Total records in Field Staging for re-analysis: {len(records)}", flush=True)

# Сопоставление для Airtable (где используются Hight/Medium/Low)
MAPPING = {"Высокий": "Hight", "Средний": "Medium", "Низкий": "Low"}

stats = {"Hight": 0, "Medium": 0, "Low": 0}
batch_payload = []

for r in records:
    rec_id = r['id']
    notes = r.get('fields', {}).get('Notes', '') or ''
    p_val_ru = parse_priority(notes)
    p_val_eng = MAPPING.get(p_val_ru, "Medium")
    
    stats[p_val_eng] += 1
    
    batch_payload.append({
        'id': rec_id,
        'fields': {
            'Priority': p_val_eng
        }
    })

print(f"Sentiment Priority Breakdown -> Hight: {stats['Hight']}, Medium: {stats['Medium']}, Low: {stats['Low']}", flush=True)

print(f"Batch updating {len(batch_payload)} records via Airtable API...", flush=True)
try:
    table.batch_update(batch_payload)
    print(f"SUCCESS: All {len(batch_payload)} records re-analyzed and updated with Priority in Airtable!", flush=True)
except Exception as e:
    # Пробуем русские имена если не прошли английские
    print(f"English values failed ({e}), trying Russian values ('Высокий'/'Средний'/'Низкий')...", flush=True)
    RU_MAP = {"Hight": "Высокий", "Medium": "Средний", "Low": "Низкий"}
    batch_payload_ru = [
        {'id': item['id'], 'fields': {'Priority': RU_MAP.get(item['fields']['Priority'], item['fields']['Priority'])}}
        for item in batch_payload
    ]
    try:
        table.batch_update(batch_payload_ru)
        print(f"SUCCESS: All {len(batch_payload_ru)} records updated with Russian Priority in Airtable!", flush=True)
    except Exception as e2:
        print(f"Error updating records: {e2}", flush=True)
