"""
Разлепление заглушки "неизвестный застройщик" в таблице Developer.

Обнаружено живым чтением базы 02.08.2026: два дубля записи "Unknown"
(и несколько родственных заглушек - "Unnamed Developer", "Local Developer",
"Неизвестный застройщик", "Неизвестный частный застройщик") держали на себе
вперемешку и настоящие безымянные виллы ("Unknown Villa N"), и реальные
именованные проекты (Alaya Residences, Seven Oceans, The Heights, UV,
CASA OASIS, LASALAHORA RESORT GARDENS, Rent Hub) - то есть проекты без
всякой связи между собой оказались привязаны к одному "застройщику".

Правило (решение владельца 02.08.2026):
  - "Unknown Villa N" -> собственный "Unknown Developer N" (число из имени
    виллы), если между виллами нет доказанной связи. Доказательств не нашли
    ни в Source/Aliases (пусто у всех), ни в Telegram (папка "RR Groups",
    bio/закреп пусты) - разносим все по отдельности.
  - Реальные именованные проекты на заглушке - отвязываются без девелопера
    (угадывать юрлицо план запрещает), уходят в отчёт на ручное заполнение.
  - Уже осмысленные "девелоперы" (DIRECT OWNER, REMAX Throne, Wei Bule,
    Private Owner / Rental Agency) не трогаются - это не заглушки из
    GENERIC_EXACT_NAMES, у виллы уже есть хоть какая-то реальная зацепка.

Запуск: python tools_unmerge_unknown_developers.py           - только план
        python tools_unmerge_unknown_developers.py --apply   - применить
"""
import argparse
import re
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app.airtable_client import get_base, robust_airtable_op

# Точное совпадение имени (не подстрока!) - "DIRECT OWNER" или "Wei Bule" не
# должны сюда попасть, у них есть хоть какая-то реальная зацепка.
GENERIC_EXACT_NAMES = {
    "unknown", "unnamed developer", "local developer",
    "неизвестный застройщик", "неизвестный частный застройщик",
}

VILLA_RE = re.compile(r'unknown villa\s*(\d+)', re.IGNORECASE)


def main(apply_changes: bool):
    base = get_base()
    if not base:
        print("Не удалось подключиться к Airtable Base.")
        return

    dev_table = base.table("Developer")
    proj_table = base.table("Projects")

    dev_records = dev_table.all()
    proj_records = proj_table.all()
    dev_by_id = {r["id"]: r for r in dev_records}

    dev_project_count = {}
    for p in proj_records:
        for did in (p["fields"].get("Developer") or []):
            dev_project_count[did] = dev_project_count.get(did, 0) + 1

    rename_plan = []   # (dev_id, old_name, new_name) - уже 1:1, просто переименовать
    relink_plan = []   # (proj_id, proj_name, old_dev_id_or_None, new_dev_name) - новая запись + релинк
    detach_plan = []   # (proj_id, proj_name, old_dev_id, old_dev_name) - реальный проект, отвязать

    for p in proj_records:
        proj_name = p["fields"].get("Project Name") or ""
        dev_links = p["fields"].get("Developer") or []
        villa_match = VILLA_RE.search(proj_name)

        if not dev_links:
            if villa_match:
                relink_plan.append((p["id"], proj_name, None, f"Unknown Developer {villa_match.group(1)}"))
            continue

        if len(dev_links) != 1:
            continue

        dev_id = dev_links[0]
        dev_rec = dev_by_id.get(dev_id)
        if not dev_rec:
            continue
        dev_name = (dev_rec["fields"].get("Developer") or "").strip()

        if dev_name.lower() not in GENERIC_EXACT_NAMES:
            continue

        if villa_match:
            target_name = f"Unknown Developer {villa_match.group(1)}"
            if dev_project_count.get(dev_id, 0) == 1:
                if dev_name != target_name:
                    rename_plan.append((dev_id, dev_name, target_name))
            else:
                relink_plan.append((p["id"], proj_name, dev_id, target_name))
        else:
            detach_plan.append((p["id"], proj_name, dev_id, dev_name))

    # Заглушки, которые после разлепления не будут держать ни одного проекта.
    touched_dev_ids = {d for _, _, d, _ in relink_plan if d} | {d for _, _, d, _ in detach_plan} | {d for d, _, _ in rename_plan}
    would_be_emptied = []
    for dev_id in touched_dev_ids:
        remaining = dev_project_count.get(dev_id, 0)
        moved_away = sum(1 for _, _, d, _ in relink_plan if d == dev_id) + sum(1 for _, _, d, _ in detach_plan if d == dev_id)
        if remaining - moved_away <= 0:
            would_be_emptied.append(dev_by_id[dev_id]["fields"].get("Developer"))

    print("=== ПЛАН РАЗЛЕПЛЕНИЯ Unknown-ЗАСТРОЙЩИКОВ ===\n")

    print(f"1. Переименовать существующую запись (уже 1:1) - {len(rename_plan)}:")
    for dev_id, old_name, new_name in rename_plan:
        print(f"   [{dev_id}] '{old_name}' -> '{new_name}'")

    print(f"\n2. Создать новую запись + перелинковать проект - {len(relink_plan)}:")
    for proj_id, proj_name, old_dev_id, new_dev_name in relink_plan:
        old_desc = dev_by_id[old_dev_id]["fields"].get("Developer") if old_dev_id else "(без девелопера)"
        print(f"   '{proj_name}' ({proj_id}): было на '{old_desc}' -> новая запись '{new_dev_name}'")

    print(f"\n3. Отвязать реальный проект от заглушки (девелопер станет пустым) - {len(detach_plan)}:")
    for proj_id, proj_name, old_dev_id, old_dev_name in detach_plan:
        print(f"   '{proj_name}' ({proj_id}): было на '{old_dev_name}' -> без девелопера (заполнить вручную)")

    print(f"\n4. Заглушки, которые опустеют и годятся на ручное удаление - {len(would_be_emptied)}:")
    for name in would_be_emptied:
        print(f"   '{name}'")
    print("   (Не удаляю сам - решение владельца: оставить одну 'Unknown' на будущее, вторую и прочие пустые - вручную.)")

    if not apply_changes:
        print(f"\n[DRY RUN] Переименований: {len(rename_plan)}, новых записей: {len(relink_plan)}, "
              f"отвязок: {len(detach_plan)}. Для применения: python tools_unmerge_unknown_developers.py --apply")
        return

    print("\nПрименяю изменения...")

    renamed = 0
    for dev_id, old_name, new_name in rename_plan:
        res = robust_airtable_op(dev_table.update, dev_id, fields={"Developer": new_name})
        if res.get("id"):
            renamed += 1

    relinked = 0
    for proj_id, proj_name, old_dev_id, new_dev_name in relink_plan:
        new_rec = robust_airtable_op(dev_table.create, {"Developer": new_dev_name})
        new_id = new_rec.get("id")
        if not new_id:
            print(f"   ОШИБКА: не удалось создать девелопера '{new_dev_name}' для '{proj_name}'")
            continue
        res = robust_airtable_op(proj_table.update, proj_id, fields={"Developer": [new_id]})
        if res.get("id"):
            relinked += 1

    detached = 0
    for proj_id, proj_name, old_dev_id, old_dev_name in detach_plan:
        res = robust_airtable_op(proj_table.update, proj_id, fields={"Developer": []})
        if res.get("id"):
            detached += 1

    print(f"\nГотово: переименовано {renamed}/{len(rename_plan)}, "
          f"создано+перелинковано {relinked}/{len(relink_plan)}, "
          f"отвязано {detached}/{len(detach_plan)}.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Разлепление Unknown-застройщиков")
    parser.add_argument('--apply', action='store_true', help="Применить изменения в Airtable")
    args = parser.parse_args()
    main(apply_changes=args.apply)
