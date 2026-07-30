from dotenv import load_dotenv
load_dotenv()
from pyairtable import Api
import os
api = Api(os.environ.get('AIRTABLE_TOKEN'))
base = api.base(os.environ.get('AIRTABLE_BASE_ID'))
for p in base.table('Projects').all():
    name = p['fields'].get('Project Name', '')
    desc = p['fields'].get('Special Conditions', '')
    if 'Rise' in name or 'Re' in name or 'rise' in name.lower() or 're ' in name.lower():
        print(f"Project: {name}")
        print(f"Description: {desc}")
        print('-'*20)
