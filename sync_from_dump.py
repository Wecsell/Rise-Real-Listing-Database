import asyncio
import json
from dotenv import load_dotenv

load_dotenv()

from app.airtable_client import upsert_developer, upsert_project, upsert_unit

async def sync_from_dump():
    print("=== Загрузка данных из dump.json в Airtable ===")
    
    try:
        with open('dump_cleaned.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("Ошибка: Файл dump_cleaned.json не найден.")
        return
        
    print(f"Найдено записей для загрузки: {len(data)}")
    
    for idx, parsed_data in enumerate(data, 1):
        print(f"\n--- Запись {idx}/{len(data)} ---")
        if not parsed_data.get('is_relevant'):
            print("Пропуск: запись помечена как нерелевантная.")
            continue
            
        dev_data = parsed_data.get('Developer', {})
        proj_data = parsed_data.get('Projects', {})
        units_data = parsed_data.get('Units', [])
        gaps = parsed_data.get('Gaps', [])
        
        dev_name = dev_data.get('Developer', 'Неизвестный застройщик')
        proj_name = proj_data.get('Project Name', 'Неизвестный проект')
        print(f"Проект: {proj_name} | Застройщик: {dev_name}")
        
        # Здесь вы можете добавить input() для подтверждения каждой записи:
        # answer = input("Отправить в Airtable? (y/n/q-выход): ")
        # if answer.lower() == 'q': break
        # if answer.lower() != 'y': continue
        
        dev_id = None
        # Если ИИ не смог определить девелопера, принудительно ставим Unknown
        if not dev_data.get('Developer'):
            dev_data['Developer'] = 'Unknown'
            
        try:
            dev_id = await upsert_developer(dev_data)
            print(f"  [OK] Застройщик сохранен (ID: {dev_id})")
        except Exception as e:
            print(f"  [ERROR] Ошибка сохранения застройщика: {e}")
            continue
            
        proj_id = None
        if proj_data.get('Project Name'):
            try:
                proj_id = await upsert_project(proj_data, dev_id, gaps)
                print(f"  [OK] Проект сохранен (ID: {proj_id})")
            except Exception as e:
                print(f"  [ERROR] Ошибка сохранения проекта: {e}")
                continue
            
        if units_data:
            for i, unit in enumerate(units_data, 1):
                try:
                    unit_id = await upsert_unit(unit, proj_id, proj_data.get('Project Name', ''), gaps)
                    print(f"  [OK] Юнит {i} сохранен (ID: {unit_id})")
                except Exception as e:
                    print(f"  [ERROR] Ошибка сохранения юнита {i}: {e}")

    print("\n=== Готово! ===")

if __name__ == '__main__':
    asyncio.run(sync_from_dump())
