"""
Приведение Developer.Contacts к единому формату "+код номер" (app.phone_formatter
.format_contacts_international). Аналог tools_clean_agency_phones.py, но:
  - не про wa.me-ссылки, а про обычный международный вид;
  - не трогает не-телефонные фрагменты (сайты, @ники, "QR code", номера
    сертификатов) - меняются только куски, опознанные как телефон.

Запуск: python tools_fix_developer_phones.py           - только план
        python tools_fix_developer_phones.py --apply   - применить
"""
import argparse
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app.airtable_client import get_base, robust_airtable_op
from app.phone_formatter import format_contacts_international


def fix_developer_phones(apply_changes: bool = False):
    base = get_base()
    if not base:
        print("Не удалось подключиться к Airtable Base.")
        return

    table = base.table('Developer')
    records = table.all()
    print(f"Загружено записей из таблицы Developer: {len(records)}")

    to_update = []
    for r in records:
        rec_id = r['id']
        fields = r.get('fields', {})
        name = fields.get('Developer', 'Без названия')
        raw = fields.get('Contacts')

        raw_str = str(raw).strip() if raw else ""
        if not raw_str:
            continue

        formatted = format_contacts_international(raw_str)
        if formatted != raw_str:
            to_update.append({'id': rec_id, 'name': name, 'old': raw_str, 'new': formatted})

    print(f"Записей с изменённым форматом контактов: {len(to_update)}")
    if not to_update:
        print("Все контакты уже в едином формате.")
        return

    print("\n--- ПЛАН ПЕРЕФОРМАТИРОВАНИЯ Developer.Contacts ---")
    for i, item in enumerate(to_update, 1):
        print(f"{i}. {item['name']} (ID: {item['id']})")
        print(f"   Было:  {item['old']}")
        print(f"   Стало: {item['new']}")
        print("-" * 60)

    if not apply_changes:
        print(f"\n[DRY RUN] Найдено {len(to_update)} записей. Для применения: "
              f"python tools_fix_developer_phones.py --apply")
        return

    print(f"\nПрименяем изменения для {len(to_update)} записей в Airtable...")
    updated = 0
    for item in to_update:
        res = robust_airtable_op(table.update, item['id'], fields={'Contacts': item['new']})
        if res and res.get('id'):
            updated += 1

    print(f"Успешно обновлено записей: {updated} из {len(to_update)}.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Приведение Developer.Contacts к формату '+код номер'")
    parser.add_argument('--apply', action='store_true', help="Применить изменения в Airtable")
    args = parser.parse_args()
    fix_developer_phones(apply_changes=args.apply)
