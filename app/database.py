try:
    import asyncpg
except ImportError:
    asyncpg = None
import asyncio
import os
import logging
import json

logger = logging.getLogger("Database")
DATABASE_URL = os.environ.get('DATABASE_URL')

pool = None

async def init_db():
    global pool
    if not asyncpg:
        logger.warning("asyncpg module not found. Running without local DB logging.")
        return
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set. Running without local DB logging.")
        return
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Postgres database pool connected successfully.")
        
        # Миграция: Добавляем колонки для пакетной синхронизации, если их нет
        async with pool.acquire() as conn:
            await conn.execute("""
                ALTER TABLE extractions
                ADD COLUMN IF NOT EXISTS raw_json JSONB,
                ADD COLUMN IF NOT EXISTS sync_status VARCHAR(50) DEFAULT 'pending',
                ADD COLUMN IF NOT EXISTS retry_count INT DEFAULT 0;
            """)

            # Реестр классификаций документов (implementation_plan.md, Э2):
            # file_id -> тип документа, заполняется один раз при fallback-разборе
            # неопознанных по имени файлов, переиспользуется всеми проектами.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS document_classifications (
                    file_id VARCHAR(255) PRIMARY KEY,
                    doc_type VARCHAR(50) NOT NULL,
                    classified_by VARCHAR(50) NOT NULL,
                    model_used VARCHAR(50),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)

    except Exception as e:
        logger.error(f"Failed to connect to Postgres database: {e}")

async def check_db_ping() -> bool:
    """Выполняет реальный запрос SELECT 1 к Postgres для проверки доступности."""
    if not pool:
        return False
    try:
        async with pool.acquire() as conn:
            val = await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2.0)
            return val == 1
    except Exception as e:
        logger.error(f"DB Ping failed: {e}")
        return False

async def save_message(msg_id: int, chat_id: int, sender_id: int, text: str, has_media: bool):
    """Сохраняет сырое сообщение из Telegram в локальную БД."""
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO messages (id, chat_id, sender_id, text, has_media)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id, chat_id) DO NOTHING
            """, msg_id, chat_id, sender_id or 0, text, has_media)
    except Exception as e:
        logger.error(f"Error saving message {msg_id} (chat {chat_id}) to DB: {e}")

async def save_extraction(message_id: int, chat_id: int, project_recid: str, object_guess: str, confidence: float, slot: str, url_status: str, why: str, needs_human: bool, raw_json: dict = None):
    """Сохраняет извлечённый факт и полный JSON в БД."""
    if not pool:
        return
    try:
        # Обрезаем строки
        project_recid = (project_recid or "")[:255]
        object_guess = (object_guess or "")[:255]
        url_status = (url_status or "")[:50]
        slot = (slot or "")[:50]
        
        json_str = json.dumps(raw_json) if raw_json else None
        
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO extractions (message_id, chat_id, project_recid, object_guess, confidence, slot, url_status, why, needs_human, raw_json, sync_status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending')
            """, message_id, chat_id, project_recid, object_guess, confidence, slot, url_status, why, needs_human, json_str)
    except Exception as e:
        logger.error(f"Error saving extraction for msg {message_id}: {e}")

async def save_fact(
    project_recid: str,
    old_value: str,
    new_value: str,
    fact_type: str = "price_change",
    unit_id: str = None,
    source_message_id: int = None,
    model_used: str = None,
    tokens_in: int = None,
    tokens_out: int = None
):
    """Сохраняет исторический факт изменения данных (например, цены) в таблицу facts."""
    if not pool:
        # Молча терять историю цен нельзя: без Postgres фича просто не работает,
        # и это должно быть видно в логе, а не выглядеть как успешная запись.
        logger.warning(
            f"Postgres недоступен — факт [{fact_type}] по проекту {project_recid} "
            f"({old_value} -> {new_value}) НЕ записан"
        )
        return
    try:
        # None -> "", но 0 и '0' обязаны сохраниться как есть: цена 0 это тоже
        # значение, а не отсутствие данных.
        old_str = "" if old_value is None else str(old_value)
        new_str = "" if new_value is None else str(new_value)
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO facts (project_recid, unit_id, old_value, new_value, fact_type, source_message_id, model_used, tokens_in, tokens_out)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """, project_recid[:255] if project_recid else "", unit_id, old_str, new_str, fact_type[:50] if fact_type else "price_change", source_message_id, model_used, tokens_in, tokens_out)
            logger.info(f"📊 Fact recorded [{fact_type}] for project {project_recid}: {old_value} -> {new_value}")
    except Exception as e:
        logger.error(f"Error saving fact for project {project_recid}: {e}")

async def close_db():
    global pool
    if pool:
        logger.info("Closing database pool...")
        await pool.close()
        pool = None
