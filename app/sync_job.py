import asyncio
import logging
import os
import json
import asyncpg
from dotenv import load_dotenv

load_dotenv()

from airtable_client import upsert_developer, upsert_project, upsert_unit

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SyncJob")

DATABASE_URL = os.environ.get('DATABASE_URL')
DRY_RUN = os.environ.get('DRY_RUN', '1') == '1'

async def sync_pending_extractions():
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set in .env")
        return

    logger.info("Starting weekly Airtable sync job...")
    if DRY_RUN:
        logger.info("DRY_RUN mode is ON. Will process records but not send to Airtable.")
    else:
        logger.info("PROD mode. Writing to Airtable.")

    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
    except Exception as e:
        logger.error(f"Failed to connect to DB: {e}")
        return

    async with pool.acquire() as conn:
        # Получаем все записи со статусом pending и где raw_json не null
        records = await conn.fetch("""
            SELECT id, raw_json 
            FROM extractions 
            WHERE sync_status = 'pending' AND raw_json IS NOT NULL
        """)

        if not records:
            logger.info("No pending extractions found. Sync complete.")
            return

        logger.info(f"Found {len(records)} pending records to sync.")

        synced_count = 0
        error_count = 0

        for record in records:
            rec_id = record['id']
            try:
                # Извлекаем распарсенный JSON
                raw_json = json.loads(record['raw_json']) if isinstance(record['raw_json'], str) else record['raw_json']
                
                if not DRY_RUN:
                    gaps = raw_json.get("Gaps", [])
                    dev_id = None
                    proj_id = None
                    
                    # 1. Developer
                    dev_data = raw_json.get("Developer")
                    if dev_data and dev_data.get("Developer"):
                        dev_id = await upsert_developer(dev_data)
                        
                    # 2. Project
                    proj_data = raw_json.get("Projects")
                    if proj_data and proj_data.get("Project Name"):
                        proj_id = await upsert_project(proj_data, dev_id, gaps)
                        
                    # 3. Units
                    units_data = raw_json.get("Units", [])
                    project_name = proj_data.get("Project Name") if proj_data else "UNKNOWN"
                    for unit in units_data:
                        if unit.get("Unit type") or unit.get("Bedrooms"):
                            await upsert_unit(unit, proj_id, project_name, gaps)

                # Помечаем как успешно отправленные
                await conn.execute("""
                    UPDATE extractions 
                    SET sync_status = 'synced' 
                    WHERE id = $1
                """, rec_id)
                synced_count += 1
                logger.info(f"Successfully synced record {rec_id}")

            except Exception as e:
                logger.error(f"Failed to sync record {rec_id}: {e}")
                error_count += 1
                # Помечаем как error чтобы не зависали навечно, либо оставляем pending
                await conn.execute("""
                    UPDATE extractions 
                    SET sync_status = 'error' 
                    WHERE id = $1
                """, rec_id)

        logger.info(f"Sync complete. Synced: {synced_count}, Errors: {error_count}")

if __name__ == '__main__':
    asyncio.run(sync_pending_extractions())
