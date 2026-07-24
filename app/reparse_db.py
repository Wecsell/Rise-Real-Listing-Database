import asyncio
import logging
import os
import json
import asyncpg
from dotenv import load_dotenv

load_dotenv()

from gemini_parser import parse_message

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ReparseDB")

DATABASE_URL = os.environ.get('DATABASE_URL')

async def reparse():
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set in .env")
        return

    logger.info("Connecting to DB...")
    pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with pool.acquire() as conn:
        # Получаем все пустые записи, сортируем от новых к старым (по message_id)
        records = await conn.fetch("""
            SELECT e.id as ext_id, m.text as original_text
            FROM extractions e
            JOIN messages m ON e.message_id = m.id AND e.chat_id = m.chat_id
            WHERE e.sync_status = 'pending' AND e.raw_json IS NULL
            ORDER BY m.id DESC
        """)
        
        if not records:
            logger.info("No empty pending records found! Everything is already parsed.")
            return
            
        logger.info(f"Found {len(records)} records to re-parse. Starting...")
        
        success_count = 0
        error_count = 0
        
        for idx, record in enumerate(records, 1):
            ext_id = record['ext_id']
            text = record['original_text']
            
            logger.info(f"[{idx}/{len(records)}] Parsing extraction ID: {ext_id}...")
            
            try:
                # Отправляем в нейросеть
                parsed_data = await parse_message(text)
                
                # Если Gemini вернул ошибку
                if parsed_data.get("error"):
                    logger.error(f"Gemini error for ID {ext_id}: {parsed_data.get('error')}")
                    error_count += 1
                    continue
                
                json_str = json.dumps(parsed_data)
                
                # Сохраняем обратно в базу
                await conn.execute("""
                    UPDATE extractions 
                    SET raw_json = $1 
                    WHERE id = $2
                """, json_str, ext_id)
                
                success_count += 1
                logger.info(f"✅ Success! Saved JSON for ID: {ext_id}")
                
            except Exception as e:
                logger.error(f"❌ Failed to parse ID {ext_id}: {e}")
                error_count += 1
                
            # Задержка 1.5 секунды, чтобы не словить ошибку 429 Too Many Requests от Google API
            await asyncio.sleep(1.5)
            
        logger.info(f"=== Reparse Complete ===")
        logger.info(f"Successfully fixed: {success_count} records.")
        logger.info(f"Errors: {error_count}")

if __name__ == '__main__':
    asyncio.run(reparse())
