import asyncio
import logging
import os
from telethon import TelegramClient, events
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

session_path = '/data/userbot.session'
if not os.path.exists('/data'):
    os.makedirs('data', exist_ok=True)
    session_path = 'data/userbot.session'

async def main():
    logger.info("Initializing Rise Real Bali Engine...")
    
    # 1. Подключение к Postgres
    await init_db()
    
    if DRY_RUN:
        logger.info("Running in DRY_RUN mode. Data will be saved to Postgres/Logs, but NOT written to Airtable.")

    if not API_ID or not API_HASH:
        logger.error("TG_API_ID or TG_API_HASH is missing in .env! Cannot start Telegram userbot.")
        return

    # 2. Инициализация Telethon Userbot
    client = TelegramClient(session_path, int(API_ID), API_HASH)

    @client.on(events.NewMessage)
    async def handle_new_message(event):
        chat = await event.get_chat()
        sender = await event.get_sender()
        text = event.text or ""
        has_media = event.media is not None
        
        logger.info(f"Received message [{event.id}] from Chat {chat.id}: {text[:60]}...")
        
        # Сохранение сырого сообщения в БД
        await save_message(event.id, chat.id, sender.id if sender else 0, text, has_media)
        
        # Извлечение данных через Gemini 3.6 Flash
        if text.strip():
            parsed_data = await parse_message(text)
            
            if parsed_data.get("is_relevant"):
                logger.info(f"🎯 Relevant info found! Project: {parsed_data.get('project_name')}, Price: {parsed_data.get('price_usd')}$")
                
                # Сохранение извлеченных фактов в БД
                await save_extraction(
                    message_id=event.id,
                    project_recid=parsed_data.get("project_name") or "UNKNOWN",
                    object_guess=parsed_data.get("unit_type") or "",
                    confidence=parsed_data.get("confidence", 0.8),
                    slot="price_update",
                    url_status="none",
                    why=parsed_data.get("reason", ""),
                    needs_human=True # Всегда требует подтверждения на сухих прогонах
                )
            else:
                logger.debug(f"Message ignored (not relevant): {parsed_data.get('reason')}")

    await client.start()
    logger.info("Userbot successfully connected and listening to Telegram chats!")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
