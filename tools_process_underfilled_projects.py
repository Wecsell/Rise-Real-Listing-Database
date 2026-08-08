"""
Пакетный запуск экстракции по ВСЕМ проектам в Airtable, которые:
1. Содержат хотя бы одну ссылку на материалы застройщика (DevKit Rus/Eng, Availability Chart).
2. Заполнены МЕНЕЕ чем на 60% (по списку обязательных полей из app/gaps.py).

Запуск:
    python tools_process_underfilled_projects.py          # Только список проектов, без записи
    python tools_process_underfilled_projects.py --apply  # Экстракция + запись в Airtable
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
from app.doc_pipeline import collect_project_links, combine_findings, save_findings_to_gaps
from app.gaps import REQUIRED_PROJECT_FIELDS, is_filled
import app.airtable_client as ac

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("UnderfilledProcessor")

TOTAL_REQUIRED_COUNT = len(REQUIRED_PROJECT_FIELDS) # 11 полей

def calculate_completeness(fields: Dict[str, Any]) -> float:
    """Возвращает процент заполненности от 0.0 до 1.0."""
    filled_count = sum(1 for field_key in REQUIRED_PROJECT_FIELDS if is_filled(fields.get(field_key)))
    return filled_count / TOTAL_REQUIRED_COUNT

async def main():
    parser = argparse.ArgumentParser(description="Обработка проектов с заполненностью < 60%")
    parser.add_argument("--apply", action="store_true",
                        help="Запускать экстракцию и писать в Airtable (по умолчанию — только список)")
    parser.add_argument("--check", action="store_true",
                        help="Совместимость: то же, что запуск без флагов — только список проектов")
    parser.add_argument("--force", action="store_true", help="Форсировать повторный разбор даже если разбор уже был")
    parser.add_argument("--limit", type=int, default=0, help="Лимит проектов (0 = без ограничений)")
    args = parser.parse_args()

    # Запись и зеркалирование — только по явному флагу. Прежний
    # `apply = not args.check` означал, что запуск без аргументов проходил по
    # всей базе, писал в Airtable и копировал гигабайты файлов на Drive.
    apply = args.apply and not args.check

    ac.init_cache()
    all_projects = ac.CACHE_PROJECTS

    target_projects = []
    for p in all_projects:
        fields = p.get('fields', {})
        p_name = fields.get('Project Name', 'Unnamed')
        
        # 1. Проверяем наличие ссылок на материалы
        has_link = any(fields.get(k) and str(fields.get(k)).strip().startswith('http') 
                       for k in ['Link to Developer’s Kit (Rus)', 'Link to Developer’s Kit (Eng)', 'Availability Chart'])
        if not has_link:
            continue

        # 2. Проверяем процент заполненности (< 60%)
        completeness = calculate_completeness(fields)
        if completeness >= 0.60:
            continue

        # 3. Проверяем не разбирался ли проект уже (если нет флага --force)
        gaps_str = str(fields.get('Gaps', ''))
        if not args.force and "--- AUTO: разбор документов (бот) ---" in gaps_str:
            continue

        target_projects.append((p, completeness))

    if args.limit > 0:
        target_projects = target_projects[:args.limit]

    logger.info(f"🎯 Найдено {len(target_projects)} проектов с материалами и заполненностью < 60%")
    for p, comp in target_projects:
        p_name = p.get('fields', {}).get('Project Name', 'Unnamed')
        logger.info(f"  • {p_name[:35]:36} | Заполненность: {comp*100:.1f}%")

    if not apply:
        logger.info("Режим отчёта: ничего не записано и не скопировано. "
                    "Для реальной обработки добавьте --apply.")
        return

    logger.info(f"\n🚀 НАЧИНАЕМ ПАКЕТНУЮ ЭКСТРАКЦИЮ И ЗЕРКАЛИРОВАНИЕ...")
    success_count = 0
    for i, (p, comp) in enumerate(target_projects, 1):
        rec_id = p['id']
        fields = p.get('fields', {})
        p_name = fields.get('Project Name', 'Unnamed')
        
        logger.info(f"\n--- [{i}/{len(target_projects)}] '{p_name}' (Заполненность {comp*100:.1f}%) ---")
        
        # Порядок (шахматки первыми) и дедуп по URL - в collect_project_links.
        ordered_links = collect_project_links(fields)

        # Копим находки всех ссылок и пишем ОДИН раз в конце - запись заменяет
        # секцию бота целиком, запись по разу на ссылку теряла находки всех
        # ссылок, кроме последней.
        results = []
        already_found: set = set()
        for field_name, url in ordered_links:
            try:
                logger.info(f"   ➜ [{field_name}]: {url}")
                res = await process_generic_link(
                    url,
                    project_name=p_name if apply else None,
                    exclude_fields=already_found if apply else None,
                )
                results.append(res)
                if apply:
                    closed = combine_findings([res])
                    already_found |= {prop["field"] for prop in closed["proposals"]}
            except Exception as e:
                logger.error(f"   ❌ Ошибка при обработке {url}: {e}")

        if results:
            combined = combine_findings(results)
            # False у save_findings_to_gaps может значить и "писать было нечего"
            # (текст совпал с уже записанным run_for_project), а не сбой -
            # считаем проект обработанным по факту находок, а не по записи.
            saved = await save_findings_to_gaps(rec_id, combined)
            if saved or combined["opened"] > 0 or combined["proposals"]:
                success_count += 1
                logger.info(
                    f"   ✅ Обработано '{p_name}' "
                    f"(предложений: {len(combined['proposals'])}, открыто документов: {combined['opened']}, "
                    f"запись в Airtable: {'да' if saved else 'без изменений'})"
                )

        await asyncio.sleep(2) # Задержка для защиты от таймаутов/лимитов API

    logger.info(f"\n🎉 ЗАВЕРШЕНО! Успешно обновлено проектов: {success_count}/{len(target_projects)}")

if __name__ == '__main__':
    asyncio.run(main())
