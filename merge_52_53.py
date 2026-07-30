import os
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.environ.get('AIRTABLE_TOKEN')
base_id = os.environ.get('AIRTABLE_BASE_ID')

api = Api(token)
table = api.base(base_id).table('Field Staging')

try:
    rec53 = table.get('recmBRknOKKuIyadb')
    audio_from_53 = rec53['fields'].get('Audio', [])

    if audio_from_53:
        formatted_audio = [{'url': item['url']} for item in audio_from_53 if 'url' in item]
        table.update('rec653jki6YYUsoyj', {'Audio': formatted_audio})
        print("Audio copied successfully")

    table.delete('recmBRknOKKuIyadb')
    print("Record 53 deleted successfully!")
except Exception as e:
    print(f"Result: {e}")
