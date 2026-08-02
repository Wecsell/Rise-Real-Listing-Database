"""
Отвязка 'Blank…' и 'Every Day' от Bali Baza.

Найдено 02.08.2026: эти два проекта висели на СТАРОМ дубле записи 'Bali Baza'
ещё до листинга (tools_list_baza_projects.py). Слияние дублей автоматически
перенесло все проекты дубля на итоговую запись 'Bali Baza' - в том числе и
эти два, хотя ни на партнёрском портале, ни в чате Baza - RiseReal их нет.
Решение владельца: другой застройщик, другой проект - каждому своя заглушка
'Unknown N' до выяснения, не общая (тот же довод, по которому вчера
разлепляли Unknown Villa N: непроверенная связь между проектами не даёт
права сажать их на одного застройщика).

Запуск: python tools_detach_baza_strays.py           - только план
        python tools_detach_baza_strays.py --apply   - применить
"""
import argparse
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app.airtable_client import get_base, robust_airtable_op
from app.naming import next_placeholder_name

STRAY_PROJECT_NAMES = ["Blank…", "Every Day"]


def main(apply_changes: bool):
    base = get_base()
    dev_table = base.table("Developer")
    proj_table = base.table("Projects")

    devs = dev_table.all()
    projects = proj_table.all()
    existing_names = [d["fields"].get("Developer") for d in devs]

    print("=== ОТВЯЗКА ЧУЖИХ ПРОЕКТОВ ОТ Bali Baza ===\n")

    for proj_name in STRAY_PROJECT_NAMES:
        proj = next((p for p in projects
                     if (p["fields"].get("Project Name") or "").strip() == proj_name), None)
        if not proj:
            print(f"  ! '{proj_name}' не найден - пропущено")
            continue

        new_dev_name = next_placeholder_name(None, existing_names)
        existing_names.append(new_dev_name)  # чтобы второй проект не получил тот же номер

        print(f"  '{proj_name}' ({proj['id']}): Bali Baza -> новая заглушка '{new_dev_name}'")

        if not apply_changes:
            continue

        new_dev = robust_airtable_op(dev_table.create, {"Developer": new_dev_name})
        new_id = new_dev.get("id")
        if not new_id:
            print(f"    ОШИБКА: не удалось создать '{new_dev_name}'")
            continue
        robust_airtable_op(proj_table.update, proj["id"], fields={"Developer": [new_id]})
        print(f"    -> {new_id}")

    if not apply_changes:
        print("\n[DRY RUN] Для применения: python tools_detach_baza_strays.py --apply")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Отвязать чужие проекты от Bali Baza")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(apply_changes=args.apply)
