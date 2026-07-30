import os
import asyncio
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

rec51_id = 'recy72ddyV4qzK7fm' # Id 51

import field_processor
print("Processing Record 51 with Gemini API...")
asyncio.run(field_processor.process_staging_records())

rec51 = table.get(rec51_id)
print("\n--- UPDATED RECORD 51 ---")
print("Status:", rec51['fields'].get('Status'))
print("Notes:", rec51['fields'].get('Notes'))
print("Project:", rec51['fields'].get('Project'))
print("Developer:", rec51['fields'].get('Developer'))
