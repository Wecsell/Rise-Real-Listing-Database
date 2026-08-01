import asyncio
import logging
import os
import json
import re
import telethon
import requests
from telethon import TelegramClient, events
from dotenv import load_dotenv

# Загружаем переменные окружения ДО импорта других модулей
load_dotenv()

from app.database import init_db, save_message, save_extraction
from app.gemini_parser import parse_message
from app.link_fetcher import process_generic_link
from app.history_scanner import scan_chat_metadata_and_history
from app.healthcheck import start_healthcheck_server
from telethon.tl.functions.messages import GetDialogFiltersRequest

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# httpx и telethon на уровне INFO пишут полный URL запроса, а токены стоят
# прямо в пути. При выводе в файл они оказываются на диске открытым текстом.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

logger = logging.getLogger("Listener")

PRE_FILTER_KEYWORDS = {"villa", "usd", "$", "спальн", "freehold", "leasehold", "проект", "девелопер", "project", "developer", "цена", "price", "вилла", "апартамент", "apartment", "unit", "rp", "juta"}

# Ключевые слова короче 4 символов ("usd", "rp") почти гарантированно совпадают
# как подстрока внутри обычных слов ("prepare", "used") - найдено 2026-08-01:
# 6 из 14 тестовых сообщений без единого признака недвижимости проходили фильтр.
# Матчим латиницу по границе слова; "$" - не буквенный символ, граница слова
# к нему не применима, оставляем подстрокой (сама по себе редко встречается
# вне сумм). Кириллица НАМЕРЕННО остаётся подстрокой отдельно от латиницы:
# у русских существительных именительный падеж без окончания - это корень, и
# подстрока специально ловит все падежи ("проект" -> "проекта", "проекте",
# "проектов"). Граница слова это ломает - "проект" почти никогда не стоит в
# тексте отдельным словом. Английские короткие слова такой природы не имеют.
_WORD_KEYWORDS = {kw for kw in PRE_FILTER_KEYWORDS
                  if kw.isalnum() and kw.isascii()}
_STEM_KEYWORDS = {kw for kw in PRE_FILTER_KEYWORDS
                  if kw.isalnum() and not kw.isascii()}
_SYMBOL_KEYWORDS = PRE_FILTER_KEYWORDS - _WORD_KEYWORDS - _STEM_KEYWORDS
_WORD_PATTERN = re.compile(
    r'\b(?:' + '|'.join(re.escape(kw) for kw in _WORD_KEYWORDS) + r')\b',
    re.IGNORECASE
) if _WORD_KEYWORDS else None

# "are" (единица площади земли, 1 are = 100 m2, обиходная в индонезийской
# недвижимости: "6 are" = 600 m2) убрана из PRE_FILTER_KEYWORDS отдельно от
# остальных слов: даже с границей слова это самый обычный английский глагол
# ("how are you", "we are on our way") - граница слова не спасает, когда
# ложное совпадение само является отдельным словом. Ловим только рядом с числом.
_ARE_UNIT_PATTERN = re.compile(r'\b\d+([.,]\d+)?\s*are\b', re.IGNORECASE)

def passes_prefilter(text: str) -> bool:
    text_lower = text.lower()
    if any(sym in text_lower for sym in _SYMBOL_KEYWORDS):
        return True
    if any(stem in text_lower for stem in _STEM_KEYWORDS):
        return True
    if _WORD_PATTERN and _WORD_PATTERN.search(text):
        return True
    return bool(_ARE_UNIT_PATTERN.search(text))

async def notify_admin(client, message):
    alert_token = os.environ.get('ALERT_BOT_TOKEN')
    alert_chat_id = os.environ.get('ALERT_CHAT_ID')
    
    if alert_token and alert_chat_id:
        url = f"https://api.telegram.org/bot{alert_token}/sendMessage"
        payload = {
            "chat_id": alert_chat_id,
            "text": f"🚨 ADMIN ALERT:\n{message}"
        }
        try:
            await asyncio.to_thread(requests.post, url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Failed to send admin alert via bot: {e}")
    else:
        try:
            await client.send_message('me', f"🚨 ADMIN ALERT:\n{message}")
        except Exception as e:
            logger.error(f"Failed to send admin alert via userbot: {e}")

API_ID = os.environ.get('TG_API_ID')
API_HASH = os.environ.get('TG_API_HASH')
ONLY_GROUPS = os.environ.get('ONLY_GROUPS', '1') == '1'
ALLOWED_KEYWORDS = [kw.strip().lower() for kw in os.environ.get('CHAT_KEYWORDS', '').split(',') if kw.strip()]
ALLOWED_CHAT_IDS = [int(cid.strip()) for cid in os.environ.get('ALLOWED_CHAT_IDS', '').split(',') if cid.strip()]
TARGET_FOLDER_NAME = os.environ.get('TARGET_FOLDER_NAME', '').strip()
SCAN_HISTORY_LIMIT = int(os.environ.get('SCAN_HISTORY_LIMIT', '50'))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data_dir = os.path.join(BASE_DIR, 'data')
if not os.path.exists(data_dir):
    os.makedirs(data_dir, exist_ok=True)
session_path = os.path.join(data_dir, 'userbot.session')

async def is_target_chat(chat) -> bool:
    """Проверяет, подходит ли чат под наши критерии."""
    if ONLY_GROUPS and getattr(chat, 'title', None) is None:
        return False
        
    chat_id = getattr(chat, 'id', None)
    
    # Если задан список конкретных ID чатов, используем только его (строгое совпадение)
    if ALLOWED_CHAT_IDS:
        return chat_id in ALLOWED_CHAT_IDS
        
    # Иначе используем фильтр по ключевым словам
    chat_title = (getattr(chat, 'title', '') or getattr(chat, 'first_name', '') or '').lower()
    if ALLOWED_KEYWORDS:
        return any(kw in chat_title for kw in ALLOWED_KEYWORDS)
            
    return True

async def main():
    logger.info("Initializing Rise Real Bali Engine...")
    
    await init_db()
    
    if not API_ID or not API_HASH:
        logger.error("TG_API_ID or TG_API_HASH is missing in .env!")
        return

    client = TelegramClient(session_path, int(API_ID), API_HASH)

    # Очередь для Rate Limiting Gemini (2 запроса в секунду)
    parse_queue = asyncio.Queue()

    async def check_system_health():
        tg_connected = client.is_connected()
        tg_roundtrip_ok = False
        
        if tg_connected:
            try:
                # Настоящий RPC-запрос get_me() к серверам Telegram с таймаутом 3.0с.
                # В отличие от is_user_authorized(), он НЕ кешируется в памяти Telethon
                # и упадет при бане, деактивации аккаунта или FloodWait.
                me = await asyncio.wait_for(client.get_me(), timeout=3.0)
                tg_roundtrip_ok = me is not None
            except Exception as e:
                logger.warning(f"Telegram real API ping failed: {e}")
                tg_roundtrip_ok = False

        # Настоящий запрос SELECT 1 к Postgres
        from app.database import check_db_ping
        db_ok = await check_db_ping()

        # ИСПРАВЛЕНИЕ БАГА: db_ok ТЕПЕРЬ ОБЯЗАТЕЛЕН для здоровьи сервиса!
        is_healthy = tg_connected and tg_roundtrip_ok and db_ok
        details = {
            "telegram_connected": tg_connected,
            "telegram_roundtrip_ok": tg_roundtrip_ok,
            "database_ping_ok": db_ok,
            "queue_size": parse_queue.qsize()
        }
        return is_healthy, details

    # Запуск сервера HealthCheck с передачей реальной проверки состояния
    port = int(os.environ.get('HEALTHCHECK_PORT', '8080'))
    await start_healthcheck_server(port=port, health_checker=check_system_health)
    
    async def parser_worker():
        while True:
            msg_id, chat_id, chat_title, text = await parse_queue.get()
            try:
                parsed_data = await parse_message(text, chat_title=chat_title)
                
                if parsed_data.get("is_relevant"):
                    # Сохранение в Postgres (аналитика)
                    proj_data = parsed_data.get("Projects", {})
                    project_name = proj_data.get("Project Name") or "UNKNOWN"
                    
                    logger.info(f"🎯 [{chat_title}] Found Project: {project_name}")
                    
                    await save_extraction(
                        message_id=msg_id,
                        chat_id=chat_id,
                        project_recid=project_name,
                        object_guess="Parsed via new schema",
                        confidence=parsed_data.get("confidence", 0.8),
                        slot="realtime",
                        url_status="none",
                        why=parsed_data.get("reason", ""),
                        needs_human=True,
                        raw_json=parsed_data
                    )

                # Переходим по найденным ссылкам
                urls = parsed_data.get("detected_urls", [])
                if urls:
                    # "UNKNOWN" - заглушка для save_extraction, не настоящее имя
                    # проекта. Передавать её в зеркало Drive нельзя - файлы из
                    # разных ещё не распознанных проектов легли бы в одну папку.
                    mirror_project_name = project_name if project_name != "UNKNOWN" else None
                    for url in urls:
                        logger.info(f"🔗 Detected URL: {url}")
                        link_result = await process_generic_link(
                            url, msg_id, chat_id, chat_title=chat_title,
                            project_name=mirror_project_name,
                        )
                        if link_result.get("is_private"):
                            logger.warning(
                                f"🔒 Ссылка требует доступа: {url} ({link_result.get('gaps')})"
                            )
            except Exception as e:
                logger.error(f"Error in parser worker: {e}")
                await notify_admin(client, f"Gemini Parser Worker Error:\n{e}")
            finally:
                parse_queue.task_done()
                await asyncio.sleep(0.5) # Максимум 2 запроса в секунду

    # Запускаем воркер в фоне
    asyncio.create_task(parser_worker())

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
        
        # 1.5. Проверяем команду /card <Название проекта>
        if text.strip().startswith("/card"):
            parts = text.strip().split(maxsplit=1)
            if len(parts) > 1:
                proj_query = parts[1].strip()
                try:
                    from app.card_generator import format_telegram_project_post, generate_pdf_project_card
                    import app.airtable_client as _ac
                    _ac.init_cache(force=True)
                    match = _ac.find_project_by_query(proj_query, _ac.CACHE_PROJECTS)
                    if match:
                        proj_fields = match.get('fields', {})
                        proj_id = match['id']
                        units = [
                            u['fields'] for u in _ac.CACHE_UNITS
                            if proj_id in (u.get('fields', {}).get('Project Name') or [])
                        ]
                        post_text = format_telegram_project_post(proj_fields, units=units)
                        await event.reply(post_text)
                        pdf_path = generate_pdf_project_card(proj_fields, units=units)
                        await event.reply(file=pdf_path)
                        if os.path.exists(pdf_path):
                            os.remove(pdf_path)
                    else:
                        await event.reply(f"❌ Проект '{proj_query}' не найден в базе.")
                except Exception as card_err:
                    logger.error(f"Ошибка обработки /card в listener: {card_err}")
            return

        # 2. Добавляем в очередь на парсинг
        if text.strip():
            if passes_prefilter(text):
                logger.info(f"Putting message {event.id} into Gemini queue. Queue size: {parse_queue.qsize()}")
                await parse_queue.put((event.id, chat.id, chat_title, text))
            else:
                logger.info(f"Skipping message {event.id} due to pre-filter (no real estate keywords).")

    await client.start()
    
    # Если задана папка, найдем ее ID и все чаты внутри
    if TARGET_FOLDER_NAME:
        logger.info(f"Looking for Telegram folder named: '{TARGET_FOLDER_NAME}'")
        try:
            filters_response = await client(GetDialogFiltersRequest())
            folder_id = None
            filter_list = getattr(filters_response, 'filters', filters_response)
            
            available_folders = []
            for f in filter_list:
                t = getattr(f, 'title', None)
                if hasattr(t, 'text'): available_folders.append(t.text)
                elif t: available_folders.append(str(t))
                else: available_folders.append('Unnamed')
                
            logger.info(f"Available folders in Telegram: {available_folders}")
            
            for f in filter_list:
                t = getattr(f, 'title', None)
                title_str = t.text if hasattr(t, 'text') else str(t) if t else None
                
                if title_str == TARGET_FOLDER_NAME:
                    folder_id = f.id
                    folder_obj = f
                    break
                    
            if folder_id is not None:
                logger.info(f"✅ Found folder '{TARGET_FOLDER_NAME}' (ID: {folder_id}). Extracting chats...")
                
                if hasattr(folder_obj, 'include_peers'):
                    for peer in folder_obj.include_peers:
                        try:
                            peer_id = telethon.utils.get_peer_id(peer)
                            if peer_id not in ALLOWED_CHAT_IDS:
                                ALLOWED_CHAT_IDS.append(peer_id)
                        except Exception as e:
                            pass
                            
                logger.info(f"Loaded {len(ALLOWED_CHAT_IDS)} chats from folder '{TARGET_FOLDER_NAME}'.")
            else:
                logger.warning(f"❌ Folder '{TARGET_FOLDER_NAME}' not found in your Telegram account!")
        except Exception as e:
            logger.error(f"Failed to fetch folders: {e}")

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
    # Два слушателя на одной сессии Telethon конфликтуют и дублируют разбор,
    # а значит и расходы на Gemini.
    from app.single_instance import acquire
    acquire('listener')

    asyncio.run(main())
