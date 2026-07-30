import os
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

# Ищем tableId для Field Staging через Meta API
url_meta = f"https://api.airtable.com/v0/meta/bases/{base_id}/tables"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

resp = requests.get(url_meta, headers=headers)
if resp.status_code == 200:
    tables = resp.json().get('tables', [])
    staging_table = next((t for t in tables if t['name'] == 'Field Staging'), None)
    if staging_table:
        table_id = staging_table['id']
        print(f"Найдена таблица Field Staging (ID: {table_id})")
        
        # Проверяем, есть ли уже поле Priority
        existing_fields = [f['name'] for f in staging_table.get('fields', [])]
        print("Текущие колонки:", existing_fields)
        
        if "Priority" not in existing_fields and "Приоритет" not in existing_fields:
            # Создаем новое поле Priority (Single Select)
            create_field_url = f"https://api.airtable.com/v0/meta/bases/{base_id}/tables/{table_id}/fields"
            field_data = {
                "name": "Priority",
                "type": "singleSelect",
                "options": {
                    "choices": [
                        {"name": "Высокий", "color": "redLight1"},
                        {"name": "Средний", "color": "yellowLight1"},
                        {"name": "Низкий", "color": "grayLight1"}
                    ]
                }
            }
            res = requests.post(create_field_url, headers=headers, json=field_data)
            if res.status_code == 200:
                print("🎉 Колонку 'Priority' успешно создано в Airtable!")
            else:
                print(f"Не удалось создать автоматически через API ({res.status_code}): {res.text}")
        else:
            print("Колонка Priority уже присутствует в схеме таблицы.")
    else:
        print("Таблица Field Staging не найдена в Meta API.")
else:
    print(f"Meta API вернул код {resp.status_code}: {resp.text}")
