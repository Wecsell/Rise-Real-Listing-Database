import json

print("=== Применение маппинга проектов ===")

with open('dump.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

try:
    with open('project_mapping.json', 'r', encoding='utf-8') as f:
        mapping = json.load(f)
except FileNotFoundError:
    print("Не найден файл project_mapping.json!")
    exit(1)

# Поддержка старого формата (только проекты) и нового (с ключами projects, developers)
proj_map = mapping.get('projects', mapping)
dev_map = mapping.get('developers', {})

cleaned_count = 0
for item in data:
    if item.get('is_relevant'):
        # Обработка Проектов
        p_name = item.get('Projects', {}).get('Project Name')
        if proj_map.get(p_name) in ["DELETE", "", "УДАЛИТЬ", None]:
            item['is_relevant'] = False
            print(f"Удален мусорный проект: {p_name}")
            cleaned_count += 1
            continue
        elif p_name in proj_map and proj_map[p_name] != p_name:
            item['Projects']['Project Name'] = proj_map[p_name]
            cleaned_count += 1
            
        # Обработка Девелоперов
        d_name = item.get('Developer', {}).get('Developer')
        if d_name in dev_map and dev_map[d_name] != d_name:
            if dev_map[d_name] in ["DELETE", "", "УДАЛИТЬ", None]:
                item['Developer']['Developer'] = None # Вычищаем мусорного девелопера
            else:
                item['Developer']['Developer'] = dev_map[d_name]
            cleaned_count += 1

with open('dump_cleaned.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"Готово! Внесено исправлений: {cleaned_count}")
print("Файл dump_cleaned.json обновлен. Можно запускать `python sync_from_dump.py`!")
