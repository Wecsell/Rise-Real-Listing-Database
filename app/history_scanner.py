import logging
import re
from telethon.tl.types import Channel, Chat
from database import save_message, save_extraction
from gemini_parser import parse_message
from link_fetcher import fetch_and_parse_link

logger = logging.getLogger("HistoryScanner")

URL_REGEX = r'https?://[^\s>"]+'

async def scan_chat_metadata_and_history(client, chat_entity, limit=100):
    """
    Сканирует:
    1. Описание/Bio группы.
    2. Закрепленные сообщения (Pinned messages).
    3. Последние N сообщений из истории чата.
    """
    chat_title = getattr(chat_entity, 'title', 'Chat')
    logger.info(f"🔎 Starting deep scan for group: '{chat_title}' (ID: {chat_entity.id})...")
    
    # 1. Сканируем описание группы (Group Description / Bio)
    try:
        full_chat = await client.get_entity(chat_entity.id)
        # Получаем подробную информацию о канале/чате
        full_info = await client(telethon.functions.channels.GetFullChannelRequest(channel=chat_entity)) if isinstance(chat_entity, Channel) else None
        
        description = getattr(full_info.full_chat, 'about', '') if full_info else ''
        if description:
            logger.info(f"📌 Found Group Description in '{chat_title}': {description[:100]}...")
            found_urls = re.findall(URL_REGEX, description)
            for url in found_urls:
                logger.info(f"🔗 Found URL in Group Bio: {url}")
                await fetch_and_parse_link(url, message_id=0)
    except Exception as e:
        logger.debug(f"Could not fetch full bio for {chat_title}: {e}")

    # 2. Сканируем последние N сообщений из истории чата (Backfill)
    scanned_count = 0
    relevant_count = 0
    
    async for message in client.iter_messages(chat_entity, limit=limit):
        if not message.text:
            continue
            
        scanned_count += 1
        text = message.text
        has_media = message.media is not None
        sender_id = message.sender_id or 0
        
        # Сохраняем историческое сообщение в локальную БД
        await save_message(message.id, chat_entity.id, sender_id, text, has_media)
        
        # Извлекаем данные через Gemini
        parsed_data = await parse_message(text)
        
        if parsed_data.get("is_relevant"):
            relevant_count += 1
            logger.info(f"🎯 [History Match] in '{chat_title}': Project {parsed_data.get('project_name')}, Price: {parsed_data.get('price_usd')}$")
            
            await save_extraction(
                message_id=message.id,
                project_recid=parsed_data.get("project_name") or chat_title,
                object_guess=parsed_data.get("unit_type") or "",
                confidence=parsed_data.get("confidence", 0.8),
                slot="history_backfill",
                url_status="none",
                why=parsed_data.get("reason", ""),
                needs_human=True
            )
            
        # Проверяем ссылки из сообщения
        urls = parsed_data.get("detected_urls", [])
        for url in urls:
            await fetch_and_parse_link(url, message.id)

    logger.info(f"✅ Finished scan for '{chat_title}'. Scanned {scanned_count} historical messages, found {relevant_count} relevant extractions.")
