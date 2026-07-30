import os
from pyairtable import Api
from dotenv import load_dotenv
from app.phone_formatter import format_whatsapp_link

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

records = table.all()
print(f"Total records in Field Staging for WhatsApp contact formatting: {len(records)}", flush=True)

batch_payload = []
updated_count = 0

for r in records:
    rec_id = r['id']
    contact_val = r.get('fields', {}).get('Contact')
    
    if contact_val:
        formatted = format_whatsapp_link(str(contact_val))
        if formatted != contact_val:
            updated_count += 1
            batch_payload.append({
                'id': rec_id,
                'fields': {
                    'Contact': formatted
                }
            })

print(f"Contacts requiring WhatsApp link formatting: {len(batch_payload)} of {len(records)}", flush=True)

if batch_payload:
    print(f"Batch updating {len(batch_payload)} contact fields via Airtable API...", flush=True)
    table.batch_update(batch_payload)
    print(f"SUCCESS: Updated {len(batch_payload)} contacts into clickable WhatsApp links in Airtable!", flush=True)
else:
    print("All contacts are already formatted into WhatsApp links or empty.", flush=True)
