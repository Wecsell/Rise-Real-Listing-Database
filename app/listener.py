import asyncio
import logging
import os
import telethon
from telethon import TelegramClient, events
from dotenv import load_dotenv

from database import init_db, save_message, save_extraction
from gemini_parser import parse_message
from link_fetcher import fetch_and_parse_link
from history_scanner import scan_chat_metadata_and_history
from airtable_client import upsert_developer, upsert_project, upsert_unit

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Listener")

API_ID = os.environ.get('TG_API_ID')
API_HASH = os.environ.get('TG_API_HASH')
DRY_RUN = os.environ.get('DRY_RUN', '1') == '1'
ONLY_GROUPS = os.environ.get('ONLY_GROUPS', '1') == '1'
ALLOWED_KEYWORDS = [kw.strip().lower() for kw in os.environ.get('CHAT_KEYWORDS', '').split(',') if kw.strip()]
SCAN_HISTORY_LIMIT = int(os.environ.get('SCAN_HISTORY_LIMIT', '50'))

session_path = '/data/userbot.session'
if not os.path.exists('/data'):
    os.makedirs('data', exist_ok=True)
    session_path = 'data/userbot.session'

async def is_target_chat(chat) -> bool:
    """Проверяет, подходит ли чат под наши критерии."""
    if ONLY_GROUPS and getattr(chat, 'title', None) is None:
        return False
        
    chat_title = (getattr(chat, 'title', '') or getattr(chat, 'first_name', '') or '').lower()
    
    if ALLOWED_KEYWORDS:
        return any(kw in chat_title for kw in ALLOWED_KEYWORDS)
            
    return True

async def main():
    logger.info("Initializing Rise Real Bali Engine...")
    
    await init_db()
    
    if DRY_RUN:
        logger.info("Running in DRY_RUN mode. Data saved to Postgres only, NOT to Airtable.")
    else:
        logger.info("Running in PROD mode. Data WILL be written to Airtable!")

    if not API_ID or not API_HASH:
        logger.error("TG_API_ID or TG_API_HASH is missing in .env!")
        return

    client = TelegramClient(session_path, int(API_ID), API_HASH)

    @client.on(events.NewMessage)
    async def handle_new_message(event):
        chat = await event.get_chat()
        
        if not await is_target_chat(chat):
            return
            
        chat_title = getattr(chat, 'title', 'Private Chat')
        sender = await event.get_sender()
        text = event.text or ""
        has_media = event.media is not None
        
        logger.info(f"📩 [{chat_title}] Message {event.id}: {text[:60]}...")
        
        # 1. Сохраняем сырое сообщение
        await save_message(event.id, chat.id, sender.id if sender else 0, text, has_media)
        
        # 2. Парсим через Gemini
        if text.strip():
            parsed_data = await parse_message(text)
            
            if parsed_data.get("is_relevant"):
                # Сохранение в Postgres (аналитика)
                proj_data = parsed_data.get("Projects", {})
                project_name = proj_data.get("Project Name") or "UNKNOWN"
                
                logger.info(f"🎯 [{chat_title}] Found Project: {project_name}")
                
                await save_extraction(
                    message_id=event.id,
                    chat_id=chat.id,
                    project_recid=project_name,
                    object_guess="Parsed via new schema",
                    confidence=parsed_data.get("confidence", 0.8),
                    slot="realtime",
                    url_status="none",
                    why=parsed_data.get("reason", ""),
                    needs_human=True
                )
                
                # Запись в Airtable (если не DRY_RUN)
                if not DRY_RUN:
                    try:
                        gaps = parsed_data.get("Gaps", [])
                        dev_id = None
                        proj_id = None
                        
                        dev_data = parsed_data.get("Developer")
                        if dev_data and dev_data.get("Developer"):
                            dev_id = await upsert_developer(dev_data)
                            
                        if proj_data and proj_data.get("Project Name"):
                            proj_id = await upsert_project(proj_data, dev_id, gaps)
                            
                        units_data = parsed_data.get("Units", [])
                        for unit in units_data:
                            if unit.get("Unit type") or unit.get("Bedrooms"):
                                await upsert_unit(unit, proj_id, project_name, gaps)
                                
                    except Exception as e:
                        logger.error(f"Error saving to Airtable: {e}")

            # 3. Переходим по найденным ссылкам
            urls = parsed_data.get("detected_urls", [])
            for url in urls:
                logger.info(f"🔗 Detected URL: {url}")
                await fetch_and_parse_link(url, event.id, chat.id)

    await client.start()
    
    # Сканирование истории и описаний всех целевых групп при запуске
    logger.info("=== SCANNING TARGET GROUPS (HISTORY + BIO) ===")
    target_chats = []
    async for dialog in client.iter_dialogs():
        if await is_target_chat(dialog.entity):
            target_chats.append(dialog)
            logger.info(f"✅ Target Chat: '{dialog.name}' (ID: {dialog.id})")
    
    logger.info(f"Found {len(target_chats)} target chats. Starting deep scan...")
    
    for dialog in target_chats:
        try:
            await scan_chat_metadata_and_history(client, dialog.entity, limit=SCAN_HISTORY_LIMIT)
        except Exception as scan_err:
            logger.warning(f"⚠️ Skipped history scan for '{dialog.name}': {scan_err}")
                
    logger.info("=== SCAN COMPLETE. LISTENING FOR NEW MESSAGES ===")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
