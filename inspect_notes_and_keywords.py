import os
from pyairtable import Api
from dotenv import load_dotenv
from app.priority_parser import parse_priority, HIGH_KEYWORDS, LOW_KEYWORDS

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

records = table.all()
print(f"Всего записей: {len(records)}")

high_found = 0
low_found = 0
medium_found = 0

print("\n--- АНАЛИЗ ЗАМЕТОК (NOTES) В ПОЛЯХ ---")
for r in records[:15]:  # Показываем первые 15
    fields = r.get('fields', {})
    notes = fields.get('Notes', '') or ''
    p_val = parse_priority(notes)
    print(f"Id: {fields.get('Id')} | Priority: {p_val} | Notes: {notes[:80]}...")

print("\n--- ВСЕ КЛЮЧЕВЫЕ СЛОВА ДЛЯ ВЫСОКОГО ПРИОРИТЕТА ---")
print(HIGH_KEYWORDS)
