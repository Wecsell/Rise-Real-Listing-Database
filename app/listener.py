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
                # Извлекаем данные из вложенной структуры JSON
                project_data = parsed_data.get("project", {})
                unit_data = parsed_data.get("unit", {})
                
                project_name = project_data.get("project_name") or "UNKNOWN"
                unit_type = unit_data.get("unit_type") or project_data.get("property_type") or ""
                
                logger.info(f"🎯 [{chat_title}] Project: {project_name}, Type: {unit_type}, Price: {unit_data.get('price_usd')}$")
                
                await save_extraction(
                    message_id=event.id,
                    project_recid=project_name,
                    object_guess=unit_type,
                    confidence=parsed_data.get("confidence", 0.8),
                    slot="realtime",
                    url_status="none",
                    why=parsed_data.get("reason", ""),
                    needs_human=True
                )
                
            # 3. Переходим по найденным ссылкам
            urls = parsed_data.get("detected_urls", [])
            for url in urls:
                logger.info(f"🔗 Detected URL: {url}")
                await fetch_and_parse_link(url, event.id)

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
