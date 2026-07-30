import os
import json
import asyncio
import logging
import requests
import tempfile
from dotenv import load_dotenv
from pyairtable import Api
from google import genai
from google.genai import types

load_dotenv(override=True)
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("FieldProcessor")

airtable_api = Api(os.environ.get('AIRTABLE_TOKEN'))
base = airtable_api.base(os.environ.get('AIRTABLE_BASE_ID'))
staging_table = base.table('Field Staging')

gemini_client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))

# Импорт существующей логики для синхронизации и нормализации
import sync_from_dump
from app.gemini_parser import SYSTEM_PROMPT
from app.priority_parser import build_update_fields
from app.gaps import project_gaps, unit_gaps, developer_gaps, merge_gaps

FIELD_PROMPT = SYSTEM_PROMPT + """

ВНИМАНИЕ: К тебе поступили материалы с "поля" (с улицы). Ты получишь ФОТО баннера/виллы и АУДИОЗАПИСЬ (комментарий агента).
Также тебе будут переданы GPS КООРДИНАТЫ.
Твоя задача:
1. Расшифровать аудиозапись, где агент диктует свои мысли (название, цену, кол-во спален, стадию строительства).
2. Извлечь текст с фото (имена проектов, застройщиков, телефоны, сайты).
3. Взять GPS координаты и поместить их в Project -> 'Coordinates(for Map)'.

3. ПРИОРИТЕТ ОБЪЕКТА (Priority):
   - КРИТИЧЕСКОЕ ПРАВИЛО: Приоритет определяется ИСКЛЮЧИТЕЛЬНО по личным ГОЛОСОВЫМ комментариям агента из аудио!
   - МАРКЕТИНГ С БАННЕРОВ: Рекламные слоганы и эпитеты с фото/баннера ("Luxury", "роскошный", "дизайнерский", "Exclusive", "5 звезд", "Best Villa") НЕ ПОДНИМАЮТ приоритет до "Высокий"! Это реклама застройщика, а не отзыв агента.
   - "Высокий" (Hight) — выставляется ТОЛЬКО если агент ЛИЧНО в аудиозаписи хвалит проект ("агент отмечает/считает, что проект топовый, отличный, супер, бомба, перспективный") или прямо говорит "приоритет высокий / срочно".
   - "Низкий" (Low) — выставляется при негативной оценке проекта/застройщика ("ужасная стройка", "мутный застройщик", "не нравится"), а также при УПОМИНАНИИ ЮРИДИЧЕСКИХ РИСКОВ: "зеленая земля / зеленая зона", "нет документов", "проблемы с документами", "нет разрешения / PBG".
   - "Средний" (Medium) — во всех остальных случаях (по умолчанию), включая рекламные слоганы с баннеров и состояние баннеров/заборов.

4. Свести все это в единый JSON, включив верхнеуровневое поле 'Priority': "Высокий" | "Средний" | "Низкий".
"""

def download_file(url: str, suffix: str) -> str:
    """Скачивает файл во временную директорию и возвращает путь."""
    resp = requests.get(url)
    resp.raise_for_status()
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp.write(resp.content)
    temp.close()
    return temp.name

async def process_staging_records():
    logger.info("Проверяем новые записи в Field Staging...")
    # Берем все записи со Status = New
    records = await asyncio.to_thread(staging_table.all, formula="{Status} = 'New'")
    
    if not records:
        logger.info("Нет новых записей для обработки.")
        return

    logger.info(f"Найдено новых находок: {len(records)}")

    for rec in records:
        rec_id = rec['id']
        fields = rec.get('fields', {})
        
        photos = fields.get('Photo', [])
        audios = fields.get('Audio', [])
        coords = fields.get('Coordinates', '')
        
        logger.info(f"\n--- Обработка записи {rec_id} ---")
        
        uploaded_files = []
        temp_files = []
        
        try:
            if photos:
                for idx, item in enumerate(photos, start=1):
                    url = item.get('url')
                    if url:
                        logger.info(f"Скачиваем фото #{idx}...")
                        path = await asyncio.to_thread(download_file, url, ".jpg")
                        temp_files.append(path)
                        logger.info(f"Грузим фото #{idx} в Gemini API...")
                        uploaded = await asyncio.to_thread(gemini_client.files.upload, file=path)
                        uploaded_files.append(uploaded)
                
            if audios:
                for idx, item in enumerate(audios, start=1):
                    url = item.get('url')
                    if url:
                        logger.info(f"Скачиваем аудио #{idx}...")
                        path = await asyncio.to_thread(download_file, url, ".ogg")
                        temp_files.append(path)
                        logger.info(f"Грузим аудио #{idx} в Gemini API...")
                        uploaded = await asyncio.to_thread(gemini_client.files.upload, file=path)
                        uploaded_files.append(uploaded)
                
            contents = uploaded_files.copy()
            dynamic_prompt = FIELD_PROMPT + f"\n\nGPS Координаты: {coords}"
            contents.append(dynamic_prompt)
            
            logger.info("Ждем ответа от Gemini 1.5 Pro...")
            response = await gemini_client.aio.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1
                )
            )
            
            text_resp = response.text.strip()
            logger.info(f"Raw Gemini response: {text_resp}")
            if text_resp.startswith("```"):
                import re
                text_resp = re.sub(r"^```(?:json)?\n?|```$", "", text_resp).strip()
            
            parsed = json.loads(text_resp)
            if isinstance(parsed, list) and len(parsed) > 0:
                parsed = parsed[0]
                
            # Normalize Projects if returned as a list by Gemini
            if isinstance(parsed.get('Projects'), list):
                if len(parsed['Projects']) > 0:
                    parsed['Projects'] = parsed['Projects'][0]
                else:
                    parsed['Projects'] = {}

            # Programmatically inject coordinates and location link
            if coords:
                if 'Projects' not in parsed or not isinstance(parsed['Projects'], dict):
                    parsed['Projects'] = {}
                parsed['Projects']['Coordinates(for Map)'] = coords
                parsed['Projects']['Location Link'] = f"https://www.google.com/maps?q={coords.replace(' ', '')}"

            # Показываем черновик пользователю
            dev_name = parsed.get('Developer', {}).get('Developer', 'Неизвестно')
            proj_name = parsed.get('Projects', {}).get('Project Name', 'Неизвестно')
            units = parsed.get('Units', [])
            
            print("\n=== ЧЕРНОВИК НАХОДКИ ===")
            print(f"Застройщик: {dev_name}")
            print(f"Контакты: {parsed.get('Developer', {}).get('Contacts', 'Нет данных')}")
            print(f"Проект: {proj_name}")
            print(f"Юнитов найдено: {len(units)}")
            for u in units:
                print(f"  - {u.get('Unit type', 'Unknown')} | {u.get('Bedrooms')} BR | {u.get('Price from (USD)', 'No Price')} USD")
            
            ans = 'y'
            if ans == 'y':
                logger.info("Отправляем в Airtable...")
                # Мокаем структуру dump для нашей функции
                dump_item = {
                    'is_relevant': True,
                    'Developer': parsed.get('Developer', {}),
                    'Projects': parsed.get('Projects', {}),
                    'Units': parsed.get('Units', []),
                    'original_text': f"Imported from Field Staging {rec_id}"
                }
                dev_data = dump_item['Developer']
                contacts = dev_data.get('Contacts', '')
                if isinstance(contacts, list):
                    contacts = ", ".join([str(x) for x in contacts if x])
                audio_notes = dev_data.get('Notes', '')
                
                final_notes = audio_notes if audio_notes else ""
                
                from app.airtable_client import upsert_developer, upsert_project, upsert_unit
                if not dev_data.get('Developer'): dev_data['Developer'] = 'Unknown'
                dev_id = await upsert_developer(dev_data)
                
                proj_data = dump_item['Projects']
                
                # Подгружаем координаты в проект
                if 'Coordinates' in fields:
                    proj_data['Coordinates(for Map)'] = fields['Coordinates']
                    
                # Пропуски считаем сами и подмешиваем то, что вернула модель.
                # Раньше сюда передавался пустой список, из-за чего находка с
                # баннера — заведомо неполная — уезжала в базу со статусом
                # Verified, а поле Gaps затиралось.
                proj_gaps = merge_gaps(
                    project_gaps(proj_data),
                    developer_gaps(dev_data),
                    parsed.get('Gaps'),
                )
                logger.info(f"Незаполненных полей по проекту: {len(proj_gaps)} -> {proj_gaps}")

                proj_id = await upsert_project(proj_data, dev_id, gaps=proj_gaps)

                for u in dump_item['Units']:
                    await upsert_unit(u, proj_id, proj_data.get('Project Name', 'None'), unit_gaps(u))
                    
                logger.info("Сохранено в основную базу!")
                
                # Ensure proj_id is a string, if it's somehow a list, grab the first element
                final_proj_id = None
                if isinstance(proj_id, list) and len(proj_id) > 0:
                    final_proj_id = proj_id[0]
                elif isinstance(proj_id, str):
                    final_proj_id = proj_id
                else:
                    final_proj_id = None
                
                priority = parsed.get('Priority', 'Средний')
                update_fields = build_update_fields(
                    priority=priority,
                    dev_id=dev_id,
                    final_proj_id=final_proj_id,
                    contacts=contacts,
                    final_notes=final_notes,
                    coords=coords,
                    status='Processed',
                    land_zoning=proj_data.get('Land Zoning Color'),
                    handover_permits=proj_data.get('Handover Permits'),
                )
                
                logger.info(f"Updating Field Staging with fields: {update_fields}")
                
                try:
                    await asyncio.to_thread(staging_table.update, rec_id, update_fields)
                except Exception as ex:
                    logger.error(f"Failed to update Field Staging with linked records: {ex}. Retrying without linked record IDs...")
                    safe_update = {k: v for k, v in update_fields.items() if k not in ('Developer', 'Project')}
                    await asyncio.to_thread(staging_table.update, rec_id, safe_update)
                
                logger.info(f"Обновлена запись {rec_id} в Field Staging (Status=Processed)!")
                
            elif ans == 'n':
                logger.info("Отклонено. Помечаем как Error.")
                await asyncio.to_thread(staging_table.update, rec_id, {'Status': 'Error'})
            else:
                logger.info("Пропущено.")
                
        except Exception as e:
            logger.error(f"Ошибка при обработке: {e}")
            try:
                await asyncio.to_thread(staging_table.update, rec_id, {'Status': 'Error', 'Notes': f"SYSTEM ERROR: {str(e)}"})
            except:
                await asyncio.to_thread(staging_table.update, rec_id, {'Status': 'Error'})
            
        finally:
            # Cleanup temp files
            for p in temp_files:
                if os.path.exists(p): os.remove(p)
            for f in uploaded_files:
                try:
                    await asyncio.to_thread(gemini_client.files.delete, name=f.name)
                except:
                    pass

import socket

def acquire_single_instance_lock():
    global _lock_socket
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Bind to localhost on a specific port. If already bound, another instance is running.
        _lock_socket.bind(("127.0.0.1", 48210))
    except socket.error:
        logger.error("Another instance of field_processor.py is already running. Exiting to prevent zombies!")
        import sys
        sys.exit(1)

if __name__ == '__main__':
    acquire_single_instance_lock()
    async def main_loop():
        logger.info("Запуск фонового обработчика (проверка каждые 30 секунд)...")
        while True:
            try:
                await process_staging_records()
            except Exception as e:
                logger.error(f"Сбой в главном цикле: {e}")
            await asyncio.sleep(30)
            
    asyncio.run(main_loop())
