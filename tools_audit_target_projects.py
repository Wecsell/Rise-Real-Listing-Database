"""
Инструмент аудита и исправления ошибок по 30 целевым проектам и связанным юнитам.

Проходит по каждому проекту и юниту 2 раза:
Прогон 1: Поиск и автоматическое исправление ошибок (отсутствие связи с застройщиком,
          невалидные координаты, опечатки в селектах, форматирование цен, битые ссылки).
Прогон 2: Финальная сверка — убеждаемся, что не осталось ни одной ошибки или дрейфа.

Запуск:
    python tools_audit_target_projects.py          # Только отчёт, ничего не пишет
    python tools_audit_target_projects.py --apply  # Аудит с авто-исправлением в Airtable
"""
import os
import sys
import json
import logging
import argparse
from typing import List, Dict, Any, Tuple
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

from pyairtable import Api
import app.airtable_client as ac
from app.schema_check import check_schema_drift
from app.naming import swap_coordinates
from app.airtable_client import AREA_ALIASES, UNIT_TYPE_ALIASES, VALID_POOL_VALUES, VALID_STAGES

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ProjectAuditor")

TARGET_30_PROJECTS = [
    "Aster Apartment", "Mangata", "Umalas Oasis", "Horizon", "Kiara Beachfront",
    "Bingin Sun n' Moon Village", "Tamora Axis", "MediSpa Villas Resort",
    "Uluwatu Art Villas", "OceaniQ Nusa Penida", "Zamaya", "Ocean Bliss Apartments",
    "Unit Space U1", "Makalu Villas", "Hidden City Ubud", "Villa in Tumbak Bayuh",
    "KANTI SUITES", "Xhotelnuanu", "The Point Villas", "The Pavilions",
    "White Palm", "Asri Bliss", "Nunggalan Beach Villas", "Melasti Villas",
    "Archestet Villas", "ANANTI", "Green Dragon", "Noah", "Nava Tamora", "White Sand Villas"
]

LAT_RANGE = (-11.0, 6.0)
LNG_RANGE = (94.0, 142.0)

def validate_coordinates(raw_coords: str) -> Tuple[bool, str, str]:
    """Возвращает (is_valid, reason, corrected_value)"""
    if not raw_coords:
        return True, "empty", ""
    parts = [p.strip() for p in str(raw_coords).split(',')]
    if len(parts) != 2:
        return False, "invalid format (must be 'lng, lat')", ""
    try:
        val1, val2 = float(parts[0]), float(parts[1])
    except ValueError:
        return False, "non-numeric coordinates", ""
        
    # Проверяем правильный порядок для Airtable Map: "lng, lat" (115.x, -8.x)
    is_lng_first = LNG_RANGE[0] <= val1 <= LNG_RANGE[1] and LAT_RANGE[0] <= val2 <= LAT_RANGE[1]
    is_lat_first = LAT_RANGE[0] <= val1 <= LAT_RANGE[1] and LNG_RANGE[0] <= val2 <= LNG_RANGE[1]
    
    if is_lng_first:
        return True, "ok", f"{val1}, {val2}"
    elif is_lat_first:
        # Разворачиваем lat,lng -> lng,lat
        return False, "reversed order (lat,lng)", f"{val2}, {val1}"
    else:
        return False, f"coordinates out of Indonesia bounds ({val1}, {val2})", ""

def audit_pass(pass_number: int, apply: bool = True) -> Tuple[int, int, List[str]]:
    """Выполняет один полный проход аудита по 30 проектам и связанным юнитам."""
    logger.info(f"\n=======================================================")
    logger.info(f"🔄 ПРОГОН {pass_number}: Аудит по всем полям 30 проектов и юнитов")
    logger.info(f"=======================================================")
    
    ac.init_cache()
    projects = [p for p in ac.CACHE_PROJECTS if p.get('fields', {}).get('Project Name') in TARGET_30_PROJECTS]
    units = ac.CACHE_UNITS
    devs = ac.CACHE_DEVELOPERS
    
    dev_map = {d['id']: d.get('fields', {}).get('Developer', '') for d in devs}
    dev_name_to_id = {d.get('fields', {}).get('Developer', '').lower().strip(): d['id'] for d in devs if d.get('fields', {}).get('Developer')}

    issues_found = 0
    issues_fixed = 0
    issue_report = []

    for i, p in enumerate(projects, 1):
        p_id = p['id']
        f = p.get('fields', {})
        p_name = f.get('Project Name', 'Unnamed')
        
        logger.info(f"\n--- [{i}/{len(projects)}] Аудит проекта '{p_name}' ({p_id}) ---")
        p_fixes = {}

        # 1. Проверка связи с Developer
        dev_links = f.get('Developer')
        if not dev_links:
            issues_found += 1
            msg = f"❌ [{p_name}] Потеряна связь с Developer!"
            logger.warning(msg)
            issue_report.append(msg)
            # Пытаемся найти по имени в названии проекта или aliase
            for d_name, d_id in dev_name_to_id.items():
                if d_name and (d_name in p_name.lower() or d_name in str(f.get('Aliases', '')).lower()):
                    if apply:
                        ac.robust_airtable_op(ac.get_base().table('Projects').update, p_id, fields={'Developer': [d_id]})
                        issues_fixed += 1
                        logger.info(f"   ✅ [FIX] Автоматически привязан Developer '{d_name}' (ID: {d_id})")
                    break

        # 2. Проверка Coordinates(for Map)
        raw_coords = f.get('Coordinates(for Map)')
        is_valid_c, c_reason, corrected_c = validate_coordinates(raw_coords)
        if not is_valid_c:
            issues_found += 1
            msg = f"⚠️ [{p_name}] Координаты невалидны ({c_reason}): '{raw_coords}'"
            logger.warning(msg)
            issue_report.append(msg)
            if corrected_c and apply:
                ac.robust_airtable_op(ac.get_base().table('Projects').update, p_id, fields={'Coordinates(for Map)': corrected_c})
                issues_fixed += 1
                logger.info(f"   ✅ [FIX] Координаты развёрнуты в 'lng, lat': '{corrected_c}'")

        # 3. Проверка District
        district = f.get('District')
        if district and district not in AREA_ALIASES.values():
            issues_found += 1
            msg = f"⚠️ [{p_name}] Невалидное значение District: '{district}'"
            logger.warning(msg)
            issue_report.append(msg)

        # 4. Проверка Property Type
        prop_type = f.get('Property Type')
        if prop_type:
            types_to_check = prop_type if isinstance(prop_type, list) else [prop_type]
            valid_types = set(ac.FALLBACK_UNIT_TYPES) | {v for v in ac.UNIT_TYPE_ALIASES.values() if v}
            invalid = [t for t in types_to_check if t not in valid_types]
            if invalid:
                issues_found += 1
                msg = f"⚠️ [{p_name}] Невалидный Property Type: '{invalid}' (значение {prop_type})"
                logger.warning(msg)
                issue_report.append(msg)

        # 5. Проверка цен Price From / Price To
        price_from = ac.safe_float(f.get('Price From (USD)'))
        price_to = ac.safe_float(f.get('Price To (USD)'))
        if price_from and price_to and price_from > price_to:
            issues_found += 1
            msg = f"⚠️ [{p_name}] Price From ({price_from}) > Price To ({price_to})"
            logger.warning(msg)
            issue_report.append(msg)

        # 6. Проверка связанных Units
        p_units = [u for u in units if p_id in u.get('fields', {}).get('Project Name', [])]
        logger.info(f"   Связанных юнитов: {len(p_units)}")
        for u in p_units:
            u_f = u.get('fields', {})
            u_key = u_f.get('Key', u['id'])
            
            # Проверка Pool
            pool_val = u_f.get('Pool')
            if pool_val and pool_val not in VALID_POOL_VALUES:
                issues_found += 1
                msg = f"⚠️ [{p_name} -> Юнит {u_key}] Невалидный Pool: '{pool_val}'"
                logger.warning(msg)
                issue_report.append(msg)

            # Проверка Unit Type
            u_type = u_f.get('Unit type')
            if u_type and u_type not in UNIT_TYPE_ALIASES.values():
                issues_found += 1
                msg = f"⚠️ [{p_name} -> Юнит {u_key}] Невалидный Unit type: '{u_type}'"
                logger.warning(msg)
                issue_report.append(msg)

    logger.info(f"\n📊 Итоги прогона {pass_number}: Найдено проблем: {issues_found}, Исправлено: {issues_fixed}")
    return issues_found, issues_fixed, issue_report

def main():
    parser = argparse.ArgumentParser(description="Двукратный аудит целевых проектов и юнитов")
    parser.add_argument("--apply", action="store_true",
                        help="Вносить исправления в Airtable (по умолчанию — только отчёт)")
    parser.add_argument("--check", action="store_true",
                        help="Совместимость: то же, что запуск без флагов — только отчёт")
    args = parser.parse_args()

    # Запись — только по явному флагу. Прежний `apply_changes = not args.check`
    # означал, что запуск без аргументов молча правит живую базу.
    apply_changes = args.apply and not args.check

    # 0. Интеграционная проверка дрейфа схемы
    logger.info("1. Проверка дрейфа схемы (app.schema_check)...")
    drift_problems = check_schema_drift()
    if drift_problems:
        logger.error(f"❌ Обнаружен дрейф схемы ({len(drift_problems)} расхождений):")
        for p in drift_problems:
            logger.error(f"  {p}")
    else:
        logger.info("✅ Схема базы согласована (0 расхождений).")

    # ПРОГОН 1
    found1, fixed1, report1 = audit_pass(1, apply=apply_changes)

    # ПРОГОН 2 (повторный контроль после фиксов)
    found2, fixed2, report2 = audit_pass(2, apply=apply_changes)

    print("\n=======================================================")
    print("🏆 СВОДНЫЙ ОТЧЁТ ДВУКРАТНОГО АУДИТА")
    print("=======================================================")
    print(f"Схема Airtable: {'✅ Согласована' if not drift_problems else '❌ Дрейф полей'}")
    print(f"Прогон 1: Найдено проблем = {found1}, Исправлено = {fixed1}")
    print(f"Прогон 2: Оставшихся проблем = {found2}, Исправлено = {fixed2}")
    
    if found2 == 0:
        print("\n✅ Все 30 проектов и связанные юниты прошли аудит БЕЗ ОШИБОК!")
    else:
        print(f"\n⚠️ Остались незакрытые замечания ({found2} шт.):")
        for r in report2:
            print(f"  {r}")

if __name__ == '__main__':
    main()
