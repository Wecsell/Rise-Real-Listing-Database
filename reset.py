from pyairtable import Api
import os
from dotenv import load_dotenv

load_dotenv(override=True)
t = Api(os.environ['AIRTABLE_TOKEN']).base(os.environ['AIRTABLE_BASE_ID']).table('Field Staging')
for r in t.all(formula="OR({Status} = 'Error', {Status} = 'Processed')"):
    t.update(r['id'], {'Status': 'New', 'Developer': None, 'Project': None, 'Contact': None, 'Notes': None})
print("Reset done")
