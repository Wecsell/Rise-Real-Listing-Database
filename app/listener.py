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
SCAN_HISTORY_LIMIT = int(os.environ.get('SCAN_HISTORY_LIMIT', '50')) # Сканировать последние 50 сообщений при старте

session_path = '/data/userbot.session'
if not os.path.exists('/data'):
    os.makedirs('data', exist_ok=True)
    session_path = 'data/userbot.session'

async def is_target_chat(chat) -> bool:
    if ONLY_GROUPS and getattr(chat, 'title', None) is None:
        return False
        
    chat_title = (getattr(chat, 'title', '') or getattr(chat, 'first_name', '') or '').lower()
    
    if ALLOWED_KEYWORDS:
        has_match = any(kw in chat_title for kw in ALLOWED_KEYWORDS)
        if not has_match:
            return False
            
    return True

async def main():
    logger.info("Initializing Rise Real Bali Engine with Deep History & Metadata Scanner...")
    
    await init_db()
    
    if DRY_RUN:
        logger.info("Running in DRY_RUN mode. Data will be saved to Postgres/Logs, but NOT written to Airtable.")

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
        
        await save_message(event.id, chat.id, sender.id if sender else 0, text, has_media)
        
        if text.strip():
            parsed_data = await parse_message(text)
            
            if parsed_data.get("is_relevant"):
                logger.info(f"🎯 Relevant info found in [{chat_title}]! Project: {parsed_data.get('project_name')}")
                
                await save_extraction(
                    message_id=event.id,
                    project_recid=parsed_data.get("project_name") or "UNKNOWN",
                    object_guess=parsed_data.get("unit_type") or "",
                    confidence=parsed_data.get("confidence", 0.8),
                    slot="price_update",
                    url_status="none",
                    why=parsed_data.get("reason", ""),
                    needs_human=True
                )
                
            urls = parsed_data.get("detected_urls", [])
            for url in urls:
                await fetch_and_parse_link(url, event.id)

    await client.start()
    
    logger.info("=== STARTING DEEP SCAN OF GROUP DESCRIPTIONS & HISTORY ===")
    async for dialog in client.iter_dialogs():
        if await is_target_chat(dialog.entity):
            logger.info(f"✅ Subscribed to Target Chat: '{dialog.name}' (ID: {dialog.id})")
            
            # Фоновый сканер истории и описания группы (по 50 сообщений на группу)
            try:
                await scan_chat_metadata_and_history(client, dialog.entity, limit=SCAN_HISTORY_LIMIT)
            except Exception as scan_err:
                logger.warning(f"Skipped history scan for {dialog.name}: {scan_err}")
                
    logger.info("==========================================================")
    
    logger.info("Userbot active! Real-time listener and deep history scanner online.")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
