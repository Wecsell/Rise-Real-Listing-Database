import os
import re
import asyncio
from dotenv import load_dotenv

load_dotenv()

from app.airtable_client import get_base

async def migrate_old_keys():
    base = get_base()
    if not base:
        print("Не удалось подключиться к Airtable.")
        return

    table = base.table('Units')
    records = table.all()
    
    print(f"Всего юнитов в базе: {len(records)}")
    
    updated_count = 0
    for r in records:
        key = r['fields'].get('Key', '')
        if not key:
            continue
            
        # Принудительно регенерируем ключ для всех записей
        old_key = key
        
        proj_id_list = r['fields'].get('Project Name')
        proj_name = ''
        if proj_id_list and len(proj_id_list) > 0:
            proj_table = base.table('Projects')
            try:
                proj_record = proj_table.get(proj_id_list[0])
                proj_name = proj_record['fields'].get('Project Name', '')
            except:
                pass
        
        u_type = str(r['fields'].get('Unit type', 'none')).lower()
        beds = str(r['fields'].get('Bedrooms', '0'))
        unit_no = r['fields'].get('Unit Number')
        
        view_raw = r['fields'].get('View', '')
        if isinstance(view_raw, list):
            view = '-'.join(str(v).lower() for v in view_raw)
        else:
            view = str(view_raw).lower()
        
        proj_slug = re.sub(r'[^a-z0-9-]', '', str(proj_name).lower().replace(' ', '-'))[:15]
        
        if unit_no:
            new_key = f"{proj_slug}__{str(unit_no).lower()}__{beds}br"
        else:
            view_slug = re.sub(r'[^a-z0-9]+', '-', view).strip('-')
            if view_slug and view_slug != 'none':
                new_key = f"{proj_slug}__{u_type}__{beds}br__{view_slug}"
            else:
                new_key = f"{proj_slug}__{u_type}__{beds}br"
        
        if old_key != new_key:
            print(f"Обновляем ключ: {old_key} -> {new_key}")
            try:
                table.update(r['id'], {'Key': new_key})
                updated_count += 1
            except Exception as e:
                print(f"Ошибка обновления {r['id']}: {e}")

    print(f"Готово. Обновлено ключей: {updated_count}")

if __name__ == '__main__':
    asyncio.run(migrate_old_keys())
