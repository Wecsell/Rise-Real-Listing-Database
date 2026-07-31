"""
tools_full_migration.py
=======================
Миграция данных из исходных баз в 'Base RR New Test' (appsAbRs7DnYYWFt6).

ИСТОЧНИКИ:
1. Base RR New (Copy) (appwky2xeAYElrmYl) - 193 проекта, 100 застройщиков
2. Base RR New (app2IEMPr6R3GelVP) - доп. записи/юниты

ПРАВИЛА:
- СТРУКТУРА ТАРГЕТ-БАЗЫ НЕИЗМЕННА (никаких новых полей, никакого расширения списков выборов select).
- Значения single/multiple select строго валидируются по допустимым choices из TARGET.
- Невалидные варианты отбрасываются (чтобы не вызывать ошибок 422 в Airtable).
- Вложения (Img) передаются с их оригинальными URL без изменения схемы.
- Никакие существующие записи в TARGET НЕ УДАЛЯЮТСЯ.
"""

import os
import sys
import time
import logging
from dotenv import load_dotenv
from pyairtable import Api

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("FullMigration")

load_dotenv()
TOKEN = os.environ["AIRTABLE_TOKEN"]
TARGET_BASE_ID = "appsAbRs7DnYYWFt6"
SOURCE_BASE_IDS = ["appwky2xeAYElrmYl", "app2IEMPr6R3GelVP"]

api = Api(TOKEN)
target_base = api.base(TARGET_BASE_ID)

FIELD_NAME_MAP = {
    'Район': 'District',
    'District Name': 'District',
}

COMPUTED_FIELDS = {
    'Unit ID', 'Price per m² from(USD)', 'Price per m²To(USD)',
    'Price per m²', 'Descriptor', 'Project Name (from Project)',
    'Developer (from Developer)', 'District (from Project)', 'Manual sort'
}

# Кэш разрешенных выборов (choices) из TARGET базы для защиты структуры
TARGET_SELECT_CHOICES = {}
TARGET_FIELD_TYPES = {}

def load_target_schema():
    log.info("Loading TARGET base schema to protect structure...")
    schema = target_base.schema()
    for t in schema.tables:
        TARGET_SELECT_CHOICES[t.name] = {}
        TARGET_FIELD_TYPES[t.name] = {}
        for f in t.fields:
            TARGET_FIELD_TYPES[t.name][f.name] = f.type
            if f.type in ('singleSelect', 'multipleSelects'):
                choices = {c.name for c in getattr(f.options, 'choices', [])} if hasattr(f, 'options') else set()
                TARGET_SELECT_CHOICES[t.name][f.name] = choices
    log.info("TARGET schema loaded successfully.")

def clean_field_value(table_name, fname, val):
    if val is None:
        return None

    # 1. Защита Select полей
    allowed_choices = TARGET_SELECT_CHOICES.get(table_name, {}).get(fname)
    ftype = TARGET_FIELD_TYPES.get(table_name, {}).get(fname)

    if ftype == 'singleSelect' and allowed_choices is not None:
        s_val = val.get('name') if isinstance(val, dict) else str(val)
        if s_val not in allowed_choices:
            log.debug(f"[{table_name}] Skipping choice '{s_val}' for singleSelect field '{fname}' (not in target schema)")
            return None
        return s_val

    if ftype == 'multipleSelects' and allowed_choices is not None:
        if isinstance(val, (list, tuple)):
            valid_list = []
            for item in val:
                s_item = item.get('name') if isinstance(item, dict) else str(item)
                if s_item in allowed_choices:
                    valid_list.append(s_item)
                else:
                    log.debug(f"[{table_name}] Filtered choice '{s_item}' for multipleSelects field '{fname}'")
            return valid_list if valid_list else None
        elif isinstance(val, str):
            return [val] if val in allowed_choices else None

    # 2. Очистка вложений (attachments)
    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict) and 'url' in val[0]:
        clean_atts = []
        for att in val:
            if isinstance(att, dict) and 'url' in att:
                clean_atts.append({
                    'url': att['url'],
                    'filename': att.get('filename', 'attachment')
                })
        return clean_atts if clean_atts else None

    return val

def run_migration():
    load_target_schema()

    log.info("Starting safe migration into Base RR New Test...")

    # 1. МИГРАЦИЯ DEVELOPER
    log.info("\n=== Step 1: Migrating Developers ===")
    tgt_dev_table = target_base.table('Developer')
    tgt_devs = tgt_dev_table.all()
    dev_name_to_id = {}
    for d in tgt_devs:
        name = d['fields'].get('Developer')
        if name:
            dev_name_to_id[str(name).strip().lower()] = d['id']

    for src_id in SOURCE_BASE_IDS:
        src_base = api.base(src_id)
        try:
            src_devs = src_base.table('Developer').all()
            log.info(f"Source base {src_id}: found {len(src_devs)} developers")
            for r in src_devs:
                fields = dict(r.get('fields', {}))
                dev_name = fields.get('Developer')
                if not dev_name:
                    continue
                d_key = str(dev_name).strip().lower()

                clean_fields = {}
                for k, v in fields.items():
                    if k in COMPUTED_FIELDS or k == 'Projects':
                        continue
                    k_target = FIELD_NAME_MAP.get(k, k)
                    if k_target not in TARGET_FIELD_TYPES.get('Developer', {}):
                        continue
                    cv = clean_field_value('Developer', k_target, v)
                    if cv is not None:
                        clean_fields[k_target] = cv

                if d_key in dev_name_to_id:
                    tgt_id = dev_name_to_id[d_key]
                    try:
                        tgt_dev_table.update(tgt_id, clean_fields)
                    except Exception as e:
                        log.warning(f"Error updating dev {dev_name}: {e}")
                else:
                    try:
                        created = tgt_dev_table.create(clean_fields)
                        dev_name_to_id[d_key] = created['id']
                    except Exception as e:
                        log.warning(f"Error creating dev {dev_name}: {e}")
        except Exception as e:
            log.warning(f"Error fetching Developer from {src_id}: {e}")

    # Пересобираем карту застройщиков
    tgt_devs = tgt_dev_table.all()
    dev_name_to_id = {str(d['fields'].get('Developer')).strip().lower(): d['id'] for d in tgt_devs if d['fields'].get('Developer')}
    log.info(f"Total Developers in Target: {len(tgt_devs)}")

    # 2. МИГРАЦИЯ PROJECTS
    log.info("\n=== Step 2: Migrating Projects ===")
    tgt_proj_table = target_base.table('Projects')
    tgt_projs = tgt_proj_table.all()
    proj_name_to_id = {}
    for p in tgt_projs:
        p_name = p['fields'].get('Project Name')
        if p_name:
            proj_name_to_id[str(p_name).strip().lower()] = p['id']

    for src_id in SOURCE_BASE_IDS:
        src_base = api.base(src_id)
        try:
            src_projs = src_base.table('Projects').all()
            log.info(f"Source base {src_id}: found {len(src_projs)} projects")
            for r in src_projs:
                fields = dict(r.get('fields', {}))
                proj_name = fields.get('Project Name')
                if not proj_name:
                    continue
                p_key = str(proj_name).strip().lower()

                # Маппим Developer link по имени
                raw_dev = fields.get('Developer')
                dev_ids_mapped = []
                if raw_dev:
                    if isinstance(raw_dev, list):
                        for d_item in raw_dev:
                            if isinstance(d_item, str) and d_item.lower() in dev_name_to_id:
                                dev_ids_mapped.append(dev_name_to_id[d_item.lower()])
                    elif isinstance(raw_dev, str) and raw_dev.lower() in dev_name_to_id:
                        dev_ids_mapped.append(dev_name_to_id[raw_dev.lower()])

                clean_fields = {}
                for k, v in fields.items():
                    if k in COMPUTED_FIELDS or k == 'Units':
                        continue
                    k_target = FIELD_NAME_MAP.get(k, k)
                    if k_target == 'Developer':
                        continue
                    if k_target not in TARGET_FIELD_TYPES.get('Projects', {}):
                        continue
                    cv = clean_field_value('Projects', k_target, v)
                    if cv is not None:
                        clean_fields[k_target] = cv

                if dev_ids_mapped:
                    clean_fields['Developer'] = dev_ids_mapped

                if p_key in proj_name_to_id:
                    tgt_id = proj_name_to_id[p_key]
                    try:
                        tgt_proj_table.update(tgt_id, clean_fields)
                    except Exception as e:
                        log.warning(f"Error updating project {proj_name}: {e}")
                else:
                    try:
                        created = tgt_proj_table.create(clean_fields)
                        proj_name_to_id[p_key] = created['id']
                    except Exception as e:
                        log.warning(f"Error creating project {proj_name}: {e}")
        except Exception as e:
            log.warning(f"Error fetching Projects from {src_id}: {e}")

    # Пересобираем карту проектов
    tgt_projs = tgt_proj_table.all()
    proj_name_to_id = {str(p['fields'].get('Project Name')).strip().lower(): p['id'] for p in tgt_projs if p['fields'].get('Project Name')}
    log.info(f"Total Projects in Target: {len(tgt_projs)}")

    # 3. МИГРАЦИЯ UNITS
    log.info("\n=== Step 3: Migrating Units ===")
    tgt_units_table = target_base.table('Units')
    tgt_units = tgt_units_table.all()
    unit_key_to_id = {}
    for u in tgt_units:
        ukey = u['fields'].get('Key') or u['fields'].get('Unit ID')
        if ukey:
            unit_key_to_id[str(ukey).strip().lower()] = u['id']

    for src_id in SOURCE_BASE_IDS:
        src_base = api.base(src_id)
        try:
            src_units = src_base.table('Units').all()
            log.info(f"Source base {src_id}: found {len(src_units)} units")
            for r in src_units:
                fields = dict(r.get('fields', {}))
                ukey = fields.get('Key') or fields.get('Unit ID')

                raw_proj = fields.get('Project Name')
                proj_ids_mapped = []
                if raw_proj:
                    if isinstance(raw_proj, list):
                        for p_item in raw_proj:
                            if isinstance(p_item, str) and p_item.lower() in proj_name_to_id:
                                proj_ids_mapped.append(proj_name_to_id[p_item.lower()])
                    elif isinstance(raw_proj, str) and raw_proj.lower() in proj_name_to_id:
                        proj_ids_mapped.append(proj_name_to_id[raw_proj.lower()])

                clean_fields = {}
                for k, v in fields.items():
                    if k in COMPUTED_FIELDS:
                        continue
                    k_target = FIELD_NAME_MAP.get(k, k)
                    if k_target == 'Project Name':
                        continue
                    if k_target not in TARGET_FIELD_TYPES.get('Units', {}):
                        continue
                    cv = clean_field_value('Units', k_target, v)
                    if cv is not None:
                        clean_fields[k_target] = cv

                if proj_ids_mapped:
                    clean_fields['Project Name'] = proj_ids_mapped

                u_dict_key = str(ukey).strip().lower() if ukey else None
                if u_dict_key and u_dict_key in unit_key_to_id:
                    tgt_id = unit_key_to_id[u_dict_key]
                    try:
                        tgt_units_table.update(tgt_id, clean_fields)
                    except Exception as e:
                        log.warning(f"Error updating unit {ukey}: {e}")
                else:
                    try:
                        created = tgt_units_table.create(clean_fields)
                        if u_dict_key:
                            unit_key_to_id[u_dict_key] = created['id']
                    except Exception as e:
                        log.warning(f"Error creating unit {ukey}: {e}")
        except Exception as e:
            log.warning(f"Error fetching Units from {src_id}: {e}")

    log.info("\n=== SAFE FULL MIGRATION COMPLETED ===")

if __name__ == '__main__':
    run_migration()
