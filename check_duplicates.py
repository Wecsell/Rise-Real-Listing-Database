import os
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

records = table.all()
records.sort(key=lambda r: r.get('createdTime', ''))

print(f"Всего записей: {len(records)}")

last_records = records[-5:]
for r in last_records:
    rec_id = r['id']
    fields = r.get('fields', {})
    print("----------------------------------------")
    print(f"Record ID: {rec_id}")
    print(f"Airtable Id: {fields.get('Id')}")
    print(f"Developer: {fields.get('Developer')}")
    print(f"Project: {fields.get('Project')}")
    print(f"Photos count: {len(fields.get('Photo', []))}")
    print(f"Audios count: {len(fields.get('Audio', []))}")
    print(f"Coordinates: {fields.get('Coordinates')}")
    print(f"Created Time: {r.get('createdTime')}")
