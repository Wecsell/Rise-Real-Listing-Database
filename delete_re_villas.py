from dotenv import load_dotenv
load_dotenv()
from pyairtable import Api
import os
api = Api(os.environ.get('AIRTABLE_TOKEN'))
base = api.base(os.environ.get('AIRTABLE_BASE_ID'))

# Найти проект
proj_table = base.table('Projects')
unit_table = base.table('Units')
dev_table = base.table('Developer')

projects = proj_table.all(formula="FIND('Re Villas', {Project Name})")
for p in projects:
    print(f"Deleting project {p['fields'].get('Project Name')}")
    # Найти все юниты этого проекта
    units = unit_table.all(formula=f"FIND('{p['fields'].get('Project Name')}', {{Project Name}})")
    for u in units:
        print(f"  Deleting unit {u['id']}")
        unit_table.delete(u['id'])
    proj_table.delete(p['id'])

# Удалить девелопера Re Villas
devs = dev_table.all(formula="FIND('Re Villas', {Developer})")
for d in devs:
    print(f"Deleting developer {d['fields'].get('Developer')}")
    dev_table.delete(d['id'])

print("Done")
