import os
import asyncio
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

# Сбрасываем статус записи №52 на 'New', чтобы field_processor обработал её заново
table.update('rec653jki6YYUsoyj', {'Status': 'New', 'Notes': None})
print("Статус записи №52 сброшен на 'New'. Запускаем обработку через Gemini...")

# Запускаем обработчик
import field_processor
asyncio.run(field_processor.process_staging_records())

# Проверяем результат
rec52 = table.get('rec653jki6YYUsoyj')
print("\n--- ОБНОВЛЕННАЯ ЗАПИСЬ №52 ---")
print("Status:", rec52['fields'].get('Status'))
print("Notes (Расшифровка):", rec52['fields'].get('Notes'))
print("Project:", rec52['fields'].get('Project'))
print("Developer:", rec52['fields'].get('Developer'))
