import os
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

# recy72ddyV4qzK7fm = Id 51
# rec653jki6YYUsoyj = Id 52

rec51 = table.get('recy72ddyV4qzK7fm')
rec52 = table.get('rec653jki6YYUsoyj')

print("--- RECORD 51 ---")
print("Status:", rec51['fields'].get('Status'))
print("Photo:", rec51['fields'].get('Photo'))
print("Audio:", rec51['fields'].get('Audio'))
print("Notes:", rec51['fields'].get('Notes'))

print("\n--- RECORD 52 ---")
print("Status:", rec52['fields'].get('Status'))
print("Photo:", rec52['fields'].get('Photo'))
print("Audio:", rec52['fields'].get('Audio'))
print("Notes:", rec52['fields'].get('Notes'))
