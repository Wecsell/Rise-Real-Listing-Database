import os
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

records = table.all()

with open("notes_export.txt", "w", encoding="utf-8") as f:
    for r in records:
        fields = r.get('fields', {})
        rec_id = fields.get('Id')
        notes = fields.get('Notes', '') or ''
        priority = fields.get('Priority')
        f.write(f"Id: {rec_id} | Current Priority: {priority} | Notes: {notes}\n")

print("Exported notes to notes_export.txt")
