import asyncpg
import os
import logging

logger = logging.getLogger("Database")
DATABASE_URL = os.environ.get('DATABASE_URL')

pool = None

async def init_db():
    global pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set. Running without local DB logging.")
        return
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Postgres database pool connected successfully.")
    except Exception as e:
        logger.error(f"Failed to connect to Postgres database: {e}")

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

async def save_extraction(message_id: int, chat_id: int, project_recid: str, object_guess: str, confidence: float, slot: str, url_status: str, why: str, needs_human: bool):
    """Сохраняет извлечённый факт (результат парсинга) в локальную БД."""
    if not pool:
        return
    try:
        # Обрезаем строки, чтобы не превысить лимиты колонок
        project_recid = (project_recid or "")[:255]
        object_guess = (object_guess or "")[:255]
        url_status = (url_status or "")[:50]
        slot = (slot or "")[:50]
        
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO extractions (message_id, chat_id, project_recid, object_guess, confidence, slot, url_status, why, needs_human)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """, message_id, chat_id, project_recid, object_guess, confidence, slot, url_status, why, needs_human)
    except Exception as e:
        logger.error(f"Error saving extraction for msg {message_id}: {e}")
