"""
Скрипт для убирания wa.me ссылок и нормализации чистых номеров телефонов в таблице Agencies в Airtable.
Разлепливает номера и сохраняет их как ЧИСТЫЕ НОМЕРА (без https://wa.me/).
"""

import sys
import re
import argparse
import logging

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app.airtable_client import get_base, robust_airtable_op
from app.dedup import extract_phones, normalize_phone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CleanAgencyPhones")


def preprocess_glued_phones(raw_str: str) -> str:
    """Разделяет склеенные индонезийские номера и международные контакты."""
    if not raw_str:
        return ""
        
    s = raw_str.strip()
    
    # Сначала очищаем от wa.me/
    s = re.sub(r'https?://wa\.me/', '', s)
    s = re.sub(r'wa\.me/', '', s)

    if s.isdigit() and len(s) > 15:
        matches = list(re.finditer(r'(?:628\d{8,11}|62\d{9,12}|08\d{8,11})', s))
        if len(matches) > 1:
            extracted_parts = [m.group(0) for m in matches]
            return ", ".join(extracted_parts)

    parts = re.split(r'[,;\n\|]+|(?<=\d|\))\s*/\s*(?=\+|\d|\()|(?<=\d)(?=\+)|(?<=\d)(?=628\d{8})|(?<=\d)(?=08\d{8})', s)
    cleaned_parts = [p.strip() for p in parts if p.strip()]
    return ", ".join(cleaned_parts)


def format_plain_phones(contact_input: str) -> str:
    """
    Принимает строку контактов, извлекает только чистые нормализованные номера 
    (и ники вида @username) и возвращает их через запятую БЕЗ ссылок wa.me.
    """
    if not contact_input or not isinstance(contact_input, str):
        return ""
        
    preprocessed = preprocess_glued_phones(contact_input)
    parts = re.split(r'[,;\n]+', preprocessed)
    
    clean_items = []
    for part in parts:
        part_str = part.strip()
        if not part_str:
            continue
        if part_str.startswith("@"):
            clean_items.append(part_str)
        else:
            norm = normalize_phone(part_str)
            if norm and norm not in clean_items:
                clean_items.append(norm)
            elif not norm and part_str not in clean_items:
                clean_items.append(part_str)
                
    return ", ".join(clean_items)


def clean_agency_phones(apply_changes: bool = False):
    base = get_base()
    if not base:
        logger.error("Не удалось подключиться к Airtable Base.")
        return

    table = base.table('Agencies')
    records = table.all()
    logger.info(f"Загружено записей из таблицы Agencies: {len(records)}")

    to_update = []
    
    for r in records:
        rec_id = r['id']
        fields = r.get('fields', {})
        agency_name = fields.get('Agency', 'Без названия')
        raw_phone = fields.get('Phones')

        raw_str = str(raw_phone).strip() if raw_phone else ""
        if not raw_str:
            continue

        formatted = format_plain_phones(raw_str)
        
        if formatted != raw_str:
            extracted = extract_phones(formatted)
            to_update.append({
                'id': rec_id,
                'agency': agency_name,
                'old': raw_str,
                'new': formatted,
                'extracted': extracted
            })

    logger.info(f"Найдено записей со ссылками/слипшимися номерами: {len(to_update)}")

    if not to_update:
        print("Все номера в Agencies уже чистые (без ссылок wa.me)!")
        return

    print("\n--- ПЛАН ОЧИСТКИ AGENCIES (БЕЗ ССЫЛОК WA.ME) ---")
    for i, item in enumerate(to_update[:10], 1):
        print(f"{i}. Агентство: {item['agency']} (ID: {item['id']})")
        print(f"   Было:  {item['old']}")
        print(f"   Стало: {item['new']}")
        print("-" * 60)

    if not apply_changes:
        print(f"\n[DRY RUN] Найдено {len(to_update)} записей. Запустите с --apply для обновления.")
        return

    print(f"\nПрименяем очистку для {len(to_update)} записей в Airtable...")
    updated_count = 0
    for item in to_update:
        res = robust_airtable_op(table.update, item['id'], fields={'Phones': item['new']})
        if res and res.get('id'):
            updated_count += 1

    print(f"Успешно очищено записей в Airtable: {updated_count} из {len(to_update)}.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Очистка ссылок wa.me и нормализация номеров в Agencies")
    parser.add_argument('--apply', action='store_true', help="Применить изменения в Airtable")
    args = parser.parse_args()
    clean_agency_phones(apply_changes=args.apply)
