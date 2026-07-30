import os
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

records = table.all()
print(f"Всего записей в Field Staging: {len(records)}")

# Сортируем записи по дате создания, чтобы сохранить порядок
records.sort(key=lambda r: r.get('createdTime', ''))

batch = []
for idx, r in enumerate(records, start=1):
    batch.append({'id': r['id'], 'fields': {'Id': idx}})

try:
    for i in range(0, len(batch), 10):
        chunk = batch[i:i+10]
        table.batch_update(chunk)
    print("✅ Все записи в столбце 'Id' успешно перенумерованы с 1 до", len(batch))
except Exception as e:
    print(f"❌ Ошибка обновления: {e}")
