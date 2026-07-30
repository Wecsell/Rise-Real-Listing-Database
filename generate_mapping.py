import os
import json
import asyncio
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

async def generate_mapping():
    print("Чтение dump.json...")
    with open('dump.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Собираем уникальные названия
    unique_projects = set()
    unique_devs = set()
    
    for item in data:
        if item.get('is_relevant'):
            p_name = item.get('Projects', {}).get('Project Name')
            if p_name and str(p_name).strip().lower() != 'none':
                unique_projects.add(str(p_name).strip())
                
            d_name = item.get('Developer', {}).get('Developer')
            if d_name and str(d_name).strip().lower() != 'none':
                unique_devs.add(str(d_name).strip())

    projects_list = sorted(list(unique_projects))
    devs_list = sorted(list(unique_devs))
    
    print(f"Найдено {len(projects_list)} проектов и {len(devs_list)} девелоперов.")

    prompt = f"""
У меня есть списки названий проектов недвижимости и застройщиков (девелоперов), извлеченных из сообщений парсером.
Многие из них являются дубликатами из-за опечаток или склеек. 

Твоя задача — сгруппировать их и вернуть JSON с двумя словарями: "projects" и "developers".
Ключ — оригинальное (кривое) название из списка, значение — чистое короткое стандартизированное название.

Правила для проектов:
- Имя застройщика + Имя проекта (например "BAZA ORIGINS" -> должно быть "Origins")
- Имя проекта + Локация (например "ORIGINS NUANU" -> должно быть "Origins")
- Очереди (например "2 очередь" -> должно склеиться с основным названием проекта, если оно очевидно, иначе оставить как есть)
- Если название полностью бредовое или мусорное, верни "DELETE".

Правила для девелоперов:
- Склеивай опечатки (например "Bali Benefit" и "BaliBenefit" -> "BaliBenefit").
- Убирай лишние слова "group", "official", "developer".

Список проектов:
{json.dumps(projects_list, ensure_ascii=False)}

Список девелоперов:
{json.dumps(devs_list, ensure_ascii=False)}

Верни СТРОГО JSON-словарь без markdown разметки:
{{
  "projects": {{
    "BAZA ORIGINS": "Origins",
    "Origins": "Origins",
    "мусорная строка": "DELETE"
  }},
  "developers": {{
    "Bali Benefit": "BaliBenefit",
    "BaliBenefit": "BaliBenefit"
  }}
}}
"""
    
    print("Отправка в Gemini для составления маппинга (это займет 10-15 сек)...")
    try:
        response = await client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        
        text_resp = response.text.strip()
        mapping = json.loads(text_resp)
        
        # Записываем в файл
        with open('project_mapping.json', 'w', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, indent=4)
            
        print("Готово! Маппинг (проекты и девелоперы) сохранен в project_mapping.json")
        print("Откройте этот файл, проверьте глазами и поправьте ошибки перед применением!")
    except Exception as e:
        print(f"Ошибка при работе с Gemini: {e}")

if __name__ == '__main__':
    asyncio.run(generate_mapping())
