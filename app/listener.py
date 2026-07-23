import asyncio
import logging
import os
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User
from dotenv import load_dotenv

from database import init_db, save_message, save_extraction
from gemini_parser import parse_message

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Listener")

API_ID = os.environ.get('TG_API_ID')
API_HASH = os.environ.get('TG_API_HASH')
DRY_RUN = os.environ.get('DRY_RUN', '1') == '1'

# Настройки фильтрации чатов:
# ONLY_GROUPS: если True, игнорируем личные переписки с друзьями (1-on-1 DMs)
ONLY_GROUPS = os.environ.get('ONLY_GROUPS', '1') == '1'

# Список ключевых слов в названии чата (если пусто — слушает все группы)
# Пример: ["rise", "magnum", "bali", "villas", "застройщик", "брокер"]
ALLOWED_KEYWORDS = [kw.strip().lower() for kw in os.environ.get('CHAT_KEYWORDS', '').split(',') if kw.strip()]

session_path = '/data/userbot.session'
if not os.path.exists('/data'):
    os.makedirs('data', exist_ok=True)
    session_path = 'data/userbot.session'

async def is_target_chat(chat) -> bool:
    """Проверяет, подходит ли чат под наши критерии целевых парсинг-групп."""
    # 1. Игнорируем личные чаты (DMs), если включен ONLY_GROUPS
    if ONLY_GROUPS and isinstance(chat, User):
        return False
        
    chat_title = (getattr(chat, 'title', '') or getattr(chat, 'first_name', '') or '').lower()
    
    # 2. Если заданы ключевые слова, проверяем совпадение в названии чата
    if ALLOWED_KEYWORDS:
        has_match = any(kw in chat_title for kw in ALLOWED_KEYWORDS)
        if not has_match:
            return False
            
    return True

async def main():
    logger.info("Initializing Rise Real Bali Engine...")
    
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
        
        # Проверяем, целевой ли это чат
        if not await is_target_chat(chat):
            return
            
        chat_title = getattr(chat, 'title', 'Private Chat')
        sender = await event.get_sender()
        text = event.text or ""
        has_media = event.media is not None
        
        logger.info(f"📩 [{chat_title}] Message {event.id}: {text[:60]}...")
        
        # Сохраняем сообщение в базу
        await save_message(event.id, chat.id, sender.id if sender else 0, text, has_media)
        
        # Извлекаем факты через Gemini Flash
        if text.strip():
            parsed_data = await parse_message(text)
            
            if parsed_data.get("is_relevant"):
                logger.info(f"🎯 Relevant info found in [{chat_title}]! Project: {parsed_data.get('project_name')}, Price: {parsed_data.get('price_usd')}$")
                
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

    await client.start()
    
    # Дамп всех доступных чатов в лог для удобства настройки
    logger.info("=== SCANNING ALL TELEGRAM DIALOGS ===")
    async for dialog in client.iter_dialogs():
        if await is_target_chat(dialog.entity):
            logger.info(f"✅ Subscribed to Target Chat: '{dialog.name}' (ID: {dialog.id})")
        else:
            logger.debug(f"⏭️ Skipped non-target chat: '{dialog.name}' (ID: {dialog.id})")
    logger.info("====================================")
    
    logger.info("Userbot listening to configured groups!")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
