import os
from pyairtable import Api
from dotenv import load_dotenv
load_dotenv()

api = Api(os.environ['AIRTABLE_TOKEN'])
base = api.base(os.environ['AIRTABLE_BASE_ID'])

for t_name in ['Developer', 'Projects', 'Units']:
    print(f"Table: {t_name}")
    table = base.table(t_name)
    rec = table.first()
    if rec:
        print([k for k in rec['fields'].keys() if 'Kit' in k or 'm2' in k or 'Price' in k or 'Area' in k])
