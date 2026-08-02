"""
Пакетный запуск экстракции данных по ссылкам проектов (Notion, Drive, Sheets, PDF).

Проходит по всем проектам в Airtable, у которых заполнено хотя бы одно из полей:
- Link to Developer’s Kit (Rus)
- Link to Developer’s Kit (Eng)
- Availability Chart

Для каждого проекта запускается `process_generic_link`, который:
1. Выкачивает содержимое ссылки (Notion API, Google Sheets CSV, Drive folder, PDF).
2. Анализирует документ с помощью Gemini под пропуски полей (Gaps).
3. Зеркалирует рендеры на личный Google Drive (если настроен).
4. Записывает блок предложений в `Gaps` и обложку в `Img`. Ссылка застройщика
   (`Link to Developer’s Kit`) не перезаписывается: адрес зеркала уходит текстом
   в секцию Gaps, отдельного поля под него в Projects нет.

Запуск:
    python tools_batch_process_links.py                   # dry-run, без записи и без зеркалирования
    python tools_batch_process_links.py --apply --limit 20 # Обработать 20 проектов
    python tools_batch_process_links.py --apply --all      # Обработать ВСЕ 125 проектов
    python tools_batch_process_links.py --project "Mangata" # Только для одного проекта
"""
import os
import sys
import asyncio
import logging
import argparse
from typing import List, Dict, Any
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

from app.link_fetcher import process_generic_link
from app.doc_pipeline import save_findings_to_gaps
import app.airtable_client as ac

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BatchLinkProcessor")

async def process_project_links(project_record: Dict[str, Any], apply: bool = True) -> bool:
    rec_id = project_record['id']
    fields = project_record.get('fields', {})
    project_name = fields.get('Project Name', 'Unknown')
    
    links = []
    for k in ['Link to Developer’s Kit (Rus)', 'Link to Developer’s Kit (Eng)', 'Availability Chart']:
        val = fields.get(k)
        if val and str(val).strip().startswith('http'):
            links.append((k, str(val).strip()))
            
    if not links:
        return False
        
    logger.info(f"🚀 Обработка проекта '{project_name}' ({len(links)} ссылок)")
    
    any_updated = False
    for field_name, url in links:
        try:
            logger.info(f"   ➜ [{field_name}]: {url}")
            # project_name включает зеркалирование файлов на личный Drive внутри
            # process_generic_link. Без apply это была бы запись мимо "сухого"
            # прогона: копии гигабайтов уезжали на Drive при формально dry-run.
            res = await process_generic_link(url, project_name=project_name if apply else None)

            if apply:
                saved = await save_findings_to_gaps(rec_id, res)
                if saved:
                    any_updated = True
                    logger.info(f"   ✅ Записано в Airtable для '{project_name}'")
            else:
                findings_count = len(res.get("doc_findings", {}).get("proposals", [])) if res.get("doc_findings") else 0
                logger.info(f"   [DRY-RUN] Найдено предложений: {findings_count}")
        except Exception as e:
            logger.error(f"   ❌ Ошибка при обработке {url}: {e}")
            
    return any_updated

TARGET_30_PROJECTS = [
    "Aster Apartment", "Mangata", "Umalas Oasis", "Horizon", "Kiara Beachfront",
    "Bingin Sun n' Moon Village", "Tamora Axis", "MediSpa Villas Resort",
    "Uluwatu Art Villas", "OceaniQ Nusa Penida", "Zamaya", "Ocean Bliss Apartments",
    "Unit Space U1", "Makalu Villas", "Hidden City Ubud", "Villa in Tumbak Bayuh",
    "KANTI SUITES", "Xhotelnuanu", "The Point Villas", "The Pavilions",
    "White Palm", "Asri Bliss", "Nunggalan Beach Villas", "Melasti Villas",
    "Archestet Villas", "ANANTI", "Green Dragon", "Noah", "Nava Tamora", "White Sand Villas"
]

async def main():
    parser = argparse.ArgumentParser(description="Пакетная обработка ссылок проектов")
    parser.add_argument("--apply", action="store_true", help="Записывать результаты в Airtable")
    parser.add_argument("--limit", type=int, default=30, help="Максимальное количество проектов")
    parser.add_argument("--all", action="store_true", help="Обработать все проекты без ограничений")
    parser.add_argument("--project", type=str, help="Имя конкретного проекта")
    parser.add_argument("--force", action="store_true", help="Переобрабатывать даже если бот уже разбирал документы")
    
    args = parser.parse_args()
    
    ac.init_cache()
    all_projects = ac.CACHE_PROJECTS
    
    if args.project:
        matched = ac.find_project_by_query(args.project, all_projects)
        target_projects = [matched] if matched else []
    else:
        target_projects = []
        for p in all_projects:
            fields = p.get('fields', {})
            p_name = fields.get('Project Name', '')
            
            # Если не передан флаг --all, ограничиваемся только 30 выбранными проектами
            if not args.all and p_name not in TARGET_30_PROJECTS:
                continue
                
            has_link = any(fields.get(k) and str(fields.get(k)).strip().startswith('http') 
                           for k in ['Link to Developer’s Kit (Rus)', 'Link to Developer’s Kit (Eng)', 'Availability Chart'])
            if not has_link:
                continue
                
            gaps_str = str(fields.get('Gaps', ''))
            if not args.force and "--- AUTO: разбор документов (бот) ---" in gaps_str:
                continue # Уже разбирали
                
            target_projects.append(p)
            
    if not args.all and not args.project and args.limit:
        target_projects = target_projects[:args.limit]
        
    logger.info(f"🎯 Найдено {len(target_projects)} проектов из списка 30 целевых для обработки (apply={args.apply})")
    
    success_count = 0
    for i, p in enumerate(target_projects, 1):
        p_name = p.get('fields', {}).get('Project Name', 'Unknown')
        logger.info(f"\n--- [{i}/{len(target_projects)}] {p_name} ---")
        updated = await process_project_links(p, apply=args.apply)
        if updated:
            success_count += 1
        await asyncio.sleep(2) # Задержка для избежания таймаутов/лимитов API
        
    logger.info(f"\n🎉 Завершено! Успешно обновлено проектов: {success_count}/{len(target_projects)}")

if __name__ == '__main__':
    asyncio.run(main())
