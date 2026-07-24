import logging
import re
import telethon
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
    2. Последние N сообщений из истории чата.
    """
    chat_title = getattr(chat_entity, 'title', 'Chat')
    logger.info(f"🔎 Starting deep scan for group: '{chat_title}' (ID: {chat_entity.id})...")
    
    # 1. Сканируем описание группы (Group Description / Bio)
    try:
        if isinstance(chat_entity, Channel):
            full_info = await client(telethon.functions.channels.GetFullChannelRequest(channel=chat_entity))
            description = getattr(full_info.full_chat, 'about', '') or ''
        else:
            description = ''
        
        if description:
            logger.info(f"📌 Found Group Description in '{chat_title}': {description[:100]}...")
            # Парсим описание группы как обычное сообщение
            parsed_bio = await parse_message(description)
            if parsed_bio.get("is_relevant"):
                project_data = parsed_bio.get("project", {})
                await save_extraction(
                    message_id=0,
                    chat_id=chat_entity.id,
                    project_recid=project_data.get("project_name") or chat_title,
                    object_guess=project_data.get("property_type") or "",
                    confidence=parsed_bio.get("confidence", 0.7),
                    slot="group_bio",
                    url_status="none",
                    why=parsed_bio.get("reason", "From group description"),
                    needs_human=True
                )
            # Ищем ссылки в описании
            found_urls = re.findall(URL_REGEX, description)
            for url in found_urls:
                logger.info(f"🔗 Found URL in Group Bio: {url}")
                await fetch_and_parse_link(url, message_id=0, chat_id=chat_entity.id)
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
            proj_data = parsed_data.get("Projects", {})
            
            logger.info(f"🎯 [History] in '{chat_title}': Project={proj_data.get('Project Name')}, Price={proj_data.get('Price From (USD)')}$")
            
            await save_extraction(
                message_id=message.id,
                chat_id=chat_entity.id,
                project_recid=proj_data.get("Project Name") or chat_title,
                object_guess="history_backfill",
                confidence=parsed_data.get("confidence", 0.8),
                slot="history_backfill",
                url_status="none",
                why=parsed_data.get("reason", ""),
                needs_human=True,
                raw_json=parsed_data
            )
            
        # Проверяем ссылки из сообщения
        urls = parsed_data.get("detected_urls", [])
        for url in urls:
            await fetch_and_parse_link(url, message.id, chat_entity.id)

    logger.info(f"✅ Finished scan for '{chat_title}'. Scanned {scanned_count} messages, found {relevant_count} relevant.")
