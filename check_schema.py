import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')
headers = {'Authorization': f'Bearer {token}'}
res = requests.get(f'https://api.airtable.com/v0/meta/bases/{base_id}/tables', headers=headers)
with open('schema.txt', 'w', encoding='utf-8') as f:
    if res.status_code == 200:
        for table in res.json()['tables']:
            f.write(f"\nTable: {table['name']}\n")
            for field in table['fields']:
                f.write(f"  - {field['name']}: {field['type']}\n")
