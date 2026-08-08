import logging
import os
import telethon
from telethon.tl.types import Channel, Chat
from app.database import save_message, save_extraction
from app.gemini_parser import parse_message
from app.link_fetcher import extract_nested_urls, process_generic_link
from app.url_safety import redact_url

logger = logging.getLogger("HistoryScanner")

def _bounded_int_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default
    return max(1, min(value, maximum))


MAX_HISTORY_MESSAGES = _bounded_int_env("MAX_HISTORY_MESSAGES", 500, 2000)
MAX_BIO_URLS = _bounded_int_env("MAX_BIO_URLS", 10, 50)
MAX_MESSAGE_URLS = _bounded_int_env("MAX_MESSAGE_URLS", 10, 50)

async def scan_chat_metadata_and_history(client, chat_entity, limit=100):
    """
    Сканирует:
    1. Описание/Bio группы.
    2. Последние N сообщений из истории чата.
    """
    try:
        history_limit = max(1, min(int(limit), MAX_HISTORY_MESSAGES))
    except (TypeError, ValueError):
        history_limit = MAX_HISTORY_MESSAGES

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
                project_data = parsed_bio.get("Projects", {}) or {}
                await save_extraction(
                    message_id=0,
                    chat_id=chat_entity.id,
                    project_recid=project_data.get("Project Name") or chat_title,
                    object_guess=project_data.get("Property Type") or "",
                    confidence=parsed_bio.get("confidence", 0.7),
                    slot="group_bio",
                    url_status="none",
                    why=parsed_bio.get("reason", "From group description"),
                    needs_human=True,
                    raw_json=parsed_bio,
                )
            # Ищем ссылки в описании
            found_urls = extract_nested_urls(description)
            if len(found_urls) > MAX_BIO_URLS:
                logger.warning(
                    "Bio URL limit reached for %r: processing %s of %s",
                    chat_title,
                    MAX_BIO_URLS,
                    len(found_urls),
                )
            for url in found_urls[:MAX_BIO_URLS]:
                logger.info("Found URL in group bio: %s", redact_url(url))
                await process_generic_link(
                    url, message_id=0, chat_id=chat_entity.id, chat_title=chat_title
                )
    except Exception as e:
        logger.debug(f"Could not fetch full bio for {chat_title}: {e}")

    # 2. Сканируем последние N сообщений из истории чата (Backfill)
    scanned_count = 0
    relevant_count = 0
    
    async for message in client.iter_messages(chat_entity, limit=history_limit):
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
        if not isinstance(urls, list):
            urls = []
        if len(urls) > MAX_MESSAGE_URLS:
            logger.warning(
                "Message URL limit reached in chat %s message %s: processing %s of %s",
                chat_entity.id,
                message.id,
                MAX_MESSAGE_URLS,
                len(urls),
            )
        mirror_project_name = (parsed_data.get("Projects", {}) or {}).get("Project Name")
        for url in urls[:MAX_MESSAGE_URLS]:
            await process_generic_link(
                url, message.id, chat_entity.id, chat_title=chat_title,
                project_name=mirror_project_name,
            )

    logger.info(f"✅ Finished scan for '{chat_title}'. Scanned {scanned_count} messages, found {relevant_count} relevant.")
