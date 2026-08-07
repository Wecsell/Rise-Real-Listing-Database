"""
Ручное заполнение базы, пока автоматический разбор выключен (LLM_BACKEND=off).

Роль модели в пайплайне — только извлечение: превратить материалы застройщика
в словарь полей. Раскладка по таблицам, нормализация значений и пересчёт Gaps
живут в airtable_client и app.gaps и от Gemini не зависят. Поэтому «работать
локально» значит: извлечение делает человек (или ассистент, читая источники
глазами), а дальше идёт ТОТ ЖЕ путь, что у автоматического разбора.

Своих записей в Airtable инструмент не делает: всё через upsert_developer /
upsert_project / upsert_unit. Иначе ручные правки обходили бы каноны базы —
списки select, порядок координат "долгота, широта", формат Key, приведение
Construction stage — и расходились бы с тем, что позже запишет модель.

Формат входа (JSON, кодировка utf-8):

    {
      "source": "https://pcepartners.notion.site/... (кит застройщика)",
      "developer": {"Developer": "PCE", "Contacts": "Tim: https://wa.me/62..."},
      "projects": [
        {
          "Project Name": "Y-WAY Boutique Hotel",
          "District": "Seseh",
          "Handover Date": "2026-06-30",
          "units": [ {"Unit type": "Apartment", "Bedrooms": 1, ...} ],
          "secondary_units": [ ... ]
        }
      ]
    }

Ключи полей — ровно как в Airtable (см. app/schema_check.py). Поле "source"
обязательно: без указания источника значение непроверяемо, а именно на
провенансе держится доверие к базе.

Запуск: python tools_manual_intake.py data/manual/pce.json          — проверка
        python tools_manual_intake.py data/manual/pce.json --apply  — запись
"""
import argparse
import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ManualIntake")

from app import airtable_client, gaps, llm_gate
from app.schema_check import REQUIRED_FIELDS


def validate_payload(payload: dict) -> list:
    """Ошибки, из-за которых запись делать нельзя. Пустой список — можно."""
    errors = []

    if not str(payload.get("source") or "").strip():
        errors.append("Не указан 'source' — источник данных обязателен")

    projects = payload.get("projects") or []
    if not projects:
        errors.append("Нет ни одного проекта в 'projects'")

    # Ручная проверка ставится галочкой в интерфейсе базы живым человеком,
    # а не приходит файлом: иначе «проверено» означало бы лишь то, что кто-то
    # написал это в JSON (см. airtable_client.HUMAN_ONLY_FIELDS).
    for proj in projects:
        for scope in (proj, *(proj.get('units') or []), *(proj.get('secondary_units') or [])):
            if isinstance(scope, dict) and airtable_client.HUMAN_ONLY_FIELDS & scope.keys():
                errors.append(
                    f"{proj.get('Project Name', '?')}: поле 'Active' нельзя ставить через файл — "
                    f"это отметка ручной проверки, её ставят в интерфейсе Airtable"
                )
                break

    # Опечатка в имени поля не игнорируется Airtable, а роняет запись всей
    # записи с 422 (см. rules.md §3). Ловим до обращения к API.
    known = set(REQUIRED_FIELDS['Projects']) | {'units', 'secondary_units', 'Total Units',
                                                'Downpayment', 'Lease Term (years)',
                                                'Renewal Right', 'Special Conditions',
                                                'Extension Term (years)', 'Installment Notes',
                                                'Property Management', 'View', 'Location Link'}
    for proj in projects:
        name = proj.get("Project Name")
        if not name:
            errors.append("У проекта нет 'Project Name'")
            continue
        for key in proj:
            if key not in known and not airtable_client.field_exists('Projects', key):
                errors.append(f"{name}: поля {key!r} нет в таблице Projects")

    # Units — это типология, не физические лоты (rules.md, §Airtable canon).
    # 07.08.2026 так набралось 250+ лишних записей одной и той же типологии
    # (COCO Hills — 49 штук на один тип). Ловим на входе, а не чистим потом.
    for proj in projects:
        name = proj.get("Project Name", "?")
        for scope_name, scope in (("units", proj.get("units")),
                                  ("secondary_units", proj.get("secondary_units"))):
            for msg in airtable_client.find_typology_violations(scope or []):
                errors.append(f"{name} ({scope_name}): {msg}")

    return errors


def _merged_with_existing(fields: dict, dev_id: str = None) -> dict:
    """
    Поля будущей записи поверх уже существующих в Airtable.

    Пропуски считаются ПО ЗАПИСИ, а не по присланному файлу (rules.md, Step 4).
    Разница видна на частичном дозаполнении: у Rise Villas кит и шахматка в
    базе стояли, но в payload их не было — и Gaps уехали бы к застройщику
    вопросом о том, что он уже прислал.

    Существующей записи может не быть (новый проект) — тогда вернётся то же,
    что пришло.
    """
    existing = airtable_client.fuzzy_match_project(
        fields.get('Project Name'), airtable_client.CACHE_PROJECTS,
        fields.get('District'), dev_id,
    )
    match = existing[0] if isinstance(existing, tuple) else existing
    if not match:
        return fields
    return {**match.get('fields', {}), **fields}


async def run(payload: dict, apply: bool) -> int:
    if llm_gate.backend() != llm_gate.BACKEND_OFF:
        logger.info("LLM_BACKEND=%s — автоматический разбор включён, ручной ввод всё равно допустим",
                    llm_gate.backend())

    errors = validate_payload(payload)
    if errors:
        print("=== ВВОД ОТКЛОНЁН ===")
        for e in errors:
            print(f"  - {e}")
        return 1

    # force=True: обычный TTL кэша — до 10 минут, а Active могли поставить в
    # интерфейсе только что, прямо перед этим запуском. Устаревший кэш без
    # свежей галочки — тихий провал самой защиты Active, а не мелочь: ручной
    # прогон разовый, лишний запрос к Airtable здесь ничего не стоит.
    await airtable_client.init_cache_async(force=True)

    source = payload["source"]
    dev_data = dict(payload.get("developer") or {})
    projects = payload["projects"]

    print(f"=== {'ЗАПИСЬ' if apply else 'ПРОВЕРКА (ничего не пишется)'} ===")
    print(f"источник: {source}\n")

    if not apply:
        for proj in projects:
            fields = {k: v for k, v in proj.items() if k not in ('units', 'secondary_units')}
            missing = gaps.project_gaps(_merged_with_existing(fields))
            print(f"  {proj['Project Name']}")
            print(f"    полей к записи: {len(fields)}")
            print(f"    юнитов: {len(proj.get('units') or [])} первичных, "
                  f"{len(proj.get('secondary_units') or [])} вторичных")
            print(f"    останется пропусков: {missing or 'нет'}")
        print("\nДля записи добавь --apply")
        return 0

    dev_id = None
    if dev_data:
        dev_id = await airtable_client.upsert_developer(dev_data)
        print(f"  Developer {dev_data.get('Developer')!r} -> {dev_id}")

    for proj in projects:
        fields = {k: v for k, v in proj.items() if k not in ('units', 'secondary_units')}
        project_gaps = gaps.project_gaps(_merged_with_existing(fields, dev_id))
        proj_id = await airtable_client.upsert_project(fields, dev_id, project_gaps)
        print(f"  Project {fields['Project Name']!r} -> {proj_id}")
        if proj_id and airtable_client.is_project_active(proj_id):
            print("    Active — обновление проекта пропущено, поля из файла не записаны")
        elif project_gaps:
            print(f"    Gaps: {project_gaps}")

        # Вторичка пишется ПОСЛЕ первички и в свою таблицу: перепутанный порядок
        # уже приводил к тому, что вторичные записи перетирали первичные.
        written = skipped = 0
        for unit in (proj.get('units') or []):
            r = await airtable_client.upsert_unit(unit, proj_id, fields['Project Name'],
                                                  gaps.unit_gaps(unit), is_secondary=False)
            if r is airtable_client.SKIPPED_ACTIVE:
                skipped += 1
            elif r:
                written += 1
        for unit in (proj.get('secondary_units') or []):
            r = await airtable_client.upsert_unit(unit, proj_id, fields['Project Name'],
                                                  gaps.unit_gaps(unit), is_secondary=True)
            if r is airtable_client.SKIPPED_ACTIVE:
                skipped += 1
            elif r:
                written += 1
        # Печатаем то, что реально произошло, а не длину входного списка —
        # иначе Active-проект получает лживый отчёт "записано N", записав 0
        # (владелец поставил галочку, следующий прогон не должен об этом врать).
        if written:
            print(f"    юнитов записано: {written}")
        if skipped:
            print(f"    юнитов пропущено (проект Active): {skipped}")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Ручное заполнение Airtable без обращений к LLM")
    parser.add_argument("payload", help="JSON-файл с извлечёнными полями")
    parser.add_argument("--apply", action="store_true", help="Записывать в Airtable")
    args = parser.parse_args()

    with open(args.payload, encoding='utf-8') as fh:
        payload = json.load(fh)

    return asyncio.run(run(payload, args.apply))


if __name__ == '__main__':
    sys.exit(main())
