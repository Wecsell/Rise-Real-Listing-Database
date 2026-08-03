"""
Переносит ссылки на папки-зеркала из поля застройщика в Projects.Renders.

Зачем: до 02.08.2026 drive_mirror писал адрес нашего зеркала в
'Link to Developer's Kit (Rus)' - поле, где должна лежать ссылка на материалы
САМОГО застройщика. Писалось только в пустое поле, поэтому чужие ссылки не
пострадали, но смысл поля был искажён: пакетные скрипты отбирают проекты
именно по нему и на следующем прогоне выкачивали бы нашу же копию.

Что делает: находит проекты, у которых в ссылочных полях стоит ID папки из-под
корня зеркала (GDRIVE_MIRROR_ROOT_ID), переносит ссылку в Renders и очищает
поле застройщика. Renders перезаписывается только если он пуст.

Запуск:
    python tools_fix_mirror_links.py            # отчёт, ничего не пишет
    python tools_fix_mirror_links.py --apply    # перенести
"""
import argparse
import logging
import os
import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

from app.drive_auth import get_drive_service
from app.drive_mirror import FOLDER_MIME, ROOT_FOLDER_ID
import app.airtable_client as ac

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger("FixMirrorLinks")

# Поля, где ссылка на зеркало оказаться не должна.
DEVELOPER_LINK_FIELDS = [
    "Link to Developer’s Kit (Rus)",
    "Link to Developer’s Kit (Eng)",
    "Availability Chart",
]


def list_mirror_folders() -> dict:
    """id -> имя для всех папок проектов под корнем зеркала."""
    service = get_drive_service()
    folders, page = {}, None
    while True:
        resp = service.files().list(
            q=f"'{ROOT_FOLDER_ID}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
            fields="nextPageToken, files(id, name)", pageSize=200,
            supportsAllDrives=True, includeItemsFromAllDrives=True, pageToken=page,
        ).execute()
        for f in resp.get("files", []):
            folders[f["id"]] = f["name"]
        page = resp.get("nextPageToken")
        if not page:
            break
    return folders


def main() -> int:
    parser = argparse.ArgumentParser(description="Перенос ссылок зеркала в Projects.Renders")
    parser.add_argument("--apply", action="store_true",
                        help="Записывать изменения в Airtable (по умолчанию — только отчёт)")
    args = parser.parse_args()

    mirror = list_mirror_folders()
    logger.info(f"Папок-зеркал под корнем {ROOT_FOLDER_ID}: {len(mirror)}")
    if not mirror:
        logger.error("Список папок зеркала пуст — без него перенос делать нельзя.")
        return 1

    ac.init_cache()
    table = ac.get_base().table("Projects")

    planned, skipped = [], []
    for p in ac.CACHE_PROJECTS:
        fields = p.get("fields", {})
        name = fields.get("Project Name", "?")
        for key in DEVELOPER_LINK_FIELDS:
            value = str(fields.get(key, ""))
            hit = next((fid for fid in mirror if fid and fid in value), None)
            if not hit:
                continue
            current_renders = str(fields.get("Renders", "") or "")
            if current_renders and hit not in current_renders:
                skipped.append((name, key, "Renders занят другой ссылкой"))
                continue
            planned.append((p["id"], name, key, value, current_renders))

    logger.info(f"К переносу: {len(planned)}, пропущено: {len(skipped)}")
    for _, name, key, value, _ in planned:
        logger.info(f"  {name[:32]:33} {key} -> Renders  ({value})")
    for name, key, why in skipped:
        logger.warning(f"  ПРОПУСК {name[:32]:33} {key}: {why}")

    if not args.apply:
        logger.info("Отчётный режим: ничего не записано. Для переноса добавьте --apply.")
        return 0

    done = 0
    for rec_id, name, key, value, current_renders in planned:
        payload = {key: ""}
        if not current_renders:
            payload["Renders"] = value
        # robust_airtable_op отбрасывает пустые значения, поэтому очистку поля
        # шлём напрямую: нам нужно записать именно пустую строку.
        try:
            table.update(rec_id, payload)
            done += 1
            logger.info(f"✅ {name[:32]:33} {key} очищено, Renders = {value}")
        except Exception as e:
            logger.error(f"❌ {name[:32]:33} {key}: {e}")

    logger.info(f"Перенесено: {done}/{len(planned)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
