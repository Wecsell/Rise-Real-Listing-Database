"""
tools_migrate_bases.py
======================
Migrates records from 'Base RR New' (SOURCE: app2IEMPr6R3GelVP)
                  to 'Base RR New Test' (TARGET: appsAbRs7DnYYWFt6).

SOURCE tables: Developer, Projects, Units, Units (Secondary)
  (Agencies and Field Staging exist only in TARGET — skipped)

Rules:
- NEVER delete records.
- Skip formula, lookup, rollup, auto-number, created-time, manual-sort, attachment fields.
- For linked-record fields: remap by primary field name to TARGET IDs.
- Upsert by key field to avoid duplicates.
- Only write fields that exist in TARGET.
- If a SELECT value not in TARGET choices: skip that field for record (log warning).
- TARGET structure is read-only: no new fields, no new choices.
- If TARGET has duplicate values for the key field: skip those records (log warning).
"""

import os
import sys
import time
import logging
from collections import Counter
from dotenv import load_dotenv
from pyairtable import Api
from pyairtable.models.schema import FieldType

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

TOKEN = os.environ["AIRTABLE_TOKEN"]
SOURCE_BASE_ID = "app2IEMPr6R3GelVP"
TARGET_BASE_ID = "appsAbRs7DnYYWFt6"

api = Api(TOKEN)
source_base = api.base(SOURCE_BASE_ID)
target_base = api.base(TARGET_BASE_ID)

SKIP_FIELD_TYPES = {
    FieldType.FORMULA,
    FieldType.MULTIPLE_LOOKUP_VALUES,
    FieldType.ROLLUP,
    FieldType.AUTO_NUMBER,
    FieldType.CREATED_TIME,
    FieldType.LAST_MODIFIED_TIME,
    FieldType.CREATED_BY,
    FieldType.LAST_MODIFIED_BY,
    FieldType.MANUAL_SORT,
    FieldType.MULTIPLE_ATTACHMENTS,
    FieldType.BUTTON,
    FieldType.COUNT,
}


def fetch_all(base, table_name):
    return base.table(table_name).all()


def get_table_schema_info(base, table_name):
    schema = base.schema()
    tbl = next((t for t in schema.tables if t.name == table_name), None)
    if tbl is None:
        raise ValueError(f"Table not found: {table_name}")
    ft, sc, li = {}, {}, {}
    for fld in tbl.fields:
        ft[fld.name] = fld.type
        if fld.type in (FieldType.SINGLE_SELECT, FieldType.MULTIPLE_SELECTS):
            opts = getattr(fld, "options", None)
            if opts and hasattr(opts, "choices"):
                sc[fld.name] = {c.name for c in opts.choices}
        if fld.type == FieldType.MULTIPLE_RECORD_LINKS:
            opts = getattr(fld, "options", None)
            if opts and hasattr(opts, "linked_table_id"):
                li[fld.name] = opts.linked_table_id
    return ft, sc, li


def get_source_table_names():
    """Return set of table names available in SOURCE base."""
    schema = source_base.schema()
    return {t.name for t in schema.tables}


def build_cross_id_map(table_name, primary_field):
    """Build {src_record_id -> tgt_record_id} by matching on primary_field value."""
    log.info(f"  Building cross-ID map: {table_name!r} by field {primary_field!r}")
    src = fetch_all(source_base, table_name)
    tgt = fetch_all(target_base, table_name)

    # Count occurrences of each name in TARGET to detect duplicates
    tgt_name_counts = Counter()
    tgt_map = {}
    for r in tgt:
        v = r["fields"].get(primary_field)
        if v:
            name = str(v).strip()
            tgt_name_counts[name] += 1
            tgt_map[name] = r["id"]  # last one wins if duplicates

    dup_names = {n for n, c in tgt_name_counts.items() if c > 1}
    if dup_names:
        log.warning(f"    WARNING: TARGET {table_name!r} has duplicate key values: {dup_names}")
        log.warning(f"    Records with these keys in SOURCE will be skipped during upsert.")

    result, unmatch = {}, 0
    for r in src:
        v = r["fields"].get(primary_field)
        if v:
            n = str(v).strip()
            if n in tgt_map and n not in dup_names:
                result[r["id"]] = tgt_map[n]
            elif n not in dup_names:
                unmatch += 1
    log.info(f"    Mapped={len(result)}, Unmatched={unmatch}, Skipped-dups={len(dup_names)}")
    return result, dup_names


def process_fields(src_fields, tgt_ft, tgt_sc, tgt_li, linked_maps, stats):
    out = {}
    for fname, val in src_fields.items():
        if fname not in tgt_ft:
            continue
        ftype = tgt_ft[fname]
        if ftype in SKIP_FIELD_TYPES:
            continue

        if ftype == FieldType.MULTIPLE_RECORD_LINKS:
            if not val:
                out[fname] = []
                continue
            imap = (linked_maps or {}).get(fname)
            if imap is None:
                continue
            nids = [imap[s] for s in val if s in imap]
            if nids:
                out[fname] = nids
            continue

        if ftype == FieldType.SINGLE_SELECT:
            allowed = tgt_sc.get(fname, set())
            vs = val.get("name") if isinstance(val, dict) else str(val)
            if vs not in allowed:
                log.warning(f"    SELECT skip: field={fname!r} value={vs!r} not in TARGET choices")
                stats["field_warnings"] += 1
                continue
            out[fname] = vs
            continue

        if ftype == FieldType.MULTIPLE_SELECTS:
            allowed = tgt_sc.get(fname, set())
            items = val if isinstance(val, list) else [val]
            valid = []
            for it in items:
                n = it.get("name") if isinstance(it, dict) else str(it)
                if n in allowed:
                    valid.append(n)
                else:
                    log.warning(f"    MULTISEL skip: field={fname!r} choice={n!r} not in TARGET choices")
                    stats["field_warnings"] += 1
            if valid:
                out[fname] = valid
            continue

        out[fname] = val
    return out


def migrate_table(table_name, key_field, linked_maps=None, batch_size=10):
    log.info("")
    log.info("=" * 60)
    log.info(f"MIGRATING: {table_name}  key_field={key_field!r}")
    log.info("=" * 60)

    tgt_ft, tgt_sc, tgt_li = get_table_schema_info(target_base, table_name)

    # Get TARGET key duplicates to pre-skip
    tgt_recs = fetch_all(target_base, table_name)
    tgt_key_counts = Counter(
        str(r["fields"].get(key_field, "")).strip()
        for r in tgt_recs
        if r["fields"].get(key_field)
    )
    tgt_dup_keys = {k for k, c in tgt_key_counts.items() if c > 1}
    if tgt_dup_keys:
        log.warning(f"  TARGET has duplicate key values (will skip upsert for these): {tgt_dup_keys}")

    src_recs = fetch_all(source_base, table_name)
    log.info(f"  SOURCE={len(src_recs)}, TARGET_before={len(tgt_recs)}")

    stats = {
        "table": table_name,
        "source_count": len(src_recs),
        "upserted": 0,
        "skipped_records": 0,
        "field_warnings": 0,
    }

    upsert_recs = []
    for r in src_recs:
        kv = r["fields"].get(key_field)
        if not kv:
            log.warning(f"  Skip record {r['id']}: no value for key field {key_field!r}")
            stats["skipped_records"] += 1
            continue
        kv_str = str(kv).strip()
        if kv_str in tgt_dup_keys:
            log.warning(f"  Skip record key={kv_str!r}: TARGET has duplicates for this key — cannot safely upsert")
            stats["skipped_records"] += 1
            continue
        nf = process_fields(r["fields"], tgt_ft, tgt_sc, tgt_li, linked_maps, stats)
        if key_field not in nf:
            nf[key_field] = kv
        upsert_recs.append({"fields": nf})

    log.info(f"  Upserting {len(upsert_recs)} records...")
    tbl = target_base.table(table_name)
    for i in range(0, len(upsert_recs), batch_size):
        batch = upsert_recs[i:i + batch_size]
        try:
            tbl.batch_upsert(batch, key_fields=[key_field], replace=False)
            stats["upserted"] += len(batch)
            log.info(f"  Batch {i // batch_size + 1}: OK {len(batch)} records")
        except Exception as e:
            log.error(f"  Batch {i // batch_size + 1} FAILED: {e}")
            for rec in batch:
                try:
                    tbl.batch_upsert([rec], key_fields=[key_field], replace=False)
                    stats["upserted"] += 1
                except Exception as e2:
                    key_val = rec["fields"].get(key_field, "?")
                    log.error(f"    Single record FAILED key={key_val!r}: {e2}")
                    stats["skipped_records"] += 1
        time.sleep(0.25)

    log.info(
        f"  DONE: src={stats['source_count']} upserted={stats['upserted']} "
        f"skip={stats['skipped_records']} warnings={stats['field_warnings']}"
    )
    return stats


def main():
    all_stats = []

    # Check which tables exist in SOURCE
    src_tables = get_source_table_names()
    log.info(f"SOURCE tables found: {sorted(src_tables)}")
    log.info("")

    # Step 1: Developer (no links to remap)
    if "Developer" in src_tables:
        all_stats.append(migrate_table("Developer", "Developer", linked_maps={}))
    else:
        log.info("Skipping Developer — not in SOURCE")

    # Step 2: Projects (linked to Developer)
    if "Projects" in src_tables:
        dev_map, _ = build_cross_id_map("Developer", "Developer")
        all_stats.append(migrate_table(
            "Projects", "Project Name",
            linked_maps={"Developer": dev_map}
        ))
    else:
        log.info("Skipping Projects — not in SOURCE")

    # Step 3: Units (linked to Projects)
    if "Units" in src_tables:
        proj_map, _ = build_cross_id_map("Projects", "Project Name")
        all_stats.append(migrate_table(
            "Units", "Key",
            linked_maps={"Project Name": proj_map}
        ))
    else:
        log.info("Skipping Units — not in SOURCE")
        proj_map = {}

    # Step 4: Units (Secondary) (linked to Projects)
    if "Units (Secondary)" in src_tables:
        if "Units" not in src_tables:
            proj_map, _ = build_cross_id_map("Projects", "Project Name")
        all_stats.append(migrate_table(
            "Units (Secondary)", "Key",
            linked_maps={"Project Name": proj_map}
        ))
    else:
        log.info("Skipping Units (Secondary) — not in SOURCE")

    # Tables not in SOURCE — report
    for tbl_name in ["Agencies", "Field Staging"]:
        if tbl_name not in src_tables:
            log.info(f"Table {tbl_name!r} not found in SOURCE — skipping (exists only in TARGET).")

    # Summary
    log.info("")
    log.info("=" * 60)
    log.info("MIGRATION COMPLETE — SUMMARY")
    log.info("=" * 60)
    log.info(f"{'Table':<25} {'Source':>8} {'Upserted':>10} {'Skipped':>9} {'Warnings':>10}")
    log.info("-" * 65)
    total_src = total_ups = total_skip = total_warn = 0
    for s in all_stats:
        note = f"  [{s['note']}]" if s.get("note") else ""
        log.info(
            f"{s['table']:<25} {s['source_count']:>8} {s['upserted']:>10} "
            f"{s['skipped_records']:>9} {s['field_warnings']:>10}{note}"
        )
        total_src += s["source_count"]
        total_ups += s["upserted"]
        total_skip += s["skipped_records"]
        total_warn += s["field_warnings"]
    log.info("-" * 65)
    log.info(
        f"{'TOTAL':<25} {total_src:>8} {total_ups:>10} {total_skip:>9} {total_warn:>10}"
    )


if __name__ == "__main__":
    main()
