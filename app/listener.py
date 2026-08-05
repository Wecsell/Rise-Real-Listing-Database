"""Telegram ingress listener with durable, fail-closed processing."""

import asyncio
import contextlib
import logging
import os
import re

import requests
import telethon
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetDialogFiltersRequest

# Do not replace deployment-injected environment values with a local .env.
load_dotenv(override=False)

from app.access import is_allowed
from app.database import close_db, init_db, save_extraction, save_message
from app.gemini_parser import parse_message
from app.healthcheck import monitor_health_transitions, start_healthcheck_server
from app.history_scanner import scan_chat_metadata_and_history
from app.link_fetcher import process_generic_link
from app.url_safety import redact_url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# httpx and Telethon can include complete URLs in INFO logs.  Some provider
# URLs contain signed credentials or a bot token in the path.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("Listener")


PRE_FILTER_KEYWORDS = {
    "villa", "usd", "$", "спальн", "freehold", "leasehold", "проект",
    "девелопер", "project", "developer", "цена", "price", "вилл",
    "апартамент", "apartment", "unit", "rp", "juta",
}
_WORD_KEYWORDS = {keyword for keyword in PRE_FILTER_KEYWORDS if keyword.isalnum() and keyword.isascii()}
_STEM_KEYWORDS = {keyword for keyword in PRE_FILTER_KEYWORDS if keyword.isalnum() and not keyword.isascii()}
_SYMBOL_KEYWORDS = PRE_FILTER_KEYWORDS - _WORD_KEYWORDS - _STEM_KEYWORDS
_WORD_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(keyword) for keyword in _WORD_KEYWORDS) + r")\b",
    re.IGNORECASE,
) if _WORD_KEYWORDS else None
_ARE_UNIT_PATTERN = re.compile(r"\b\d+([.,]\d+)?\s*are\b", re.IGNORECASE)


def passes_prefilter(text: str) -> bool:
    """Cheap relevance filter before a Gemini call."""
    text_lower = text.lower()
    if any(symbol in text_lower for symbol in _SYMBOL_KEYWORDS):
        return True
    if any(stem in text_lower for stem in _STEM_KEYWORDS):
        return True
    if _WORD_PATTERN and _WORD_PATTERN.search(text):
        return True
    return bool(_ARE_UNIT_PATTERN.search(text))


def _bounded_int_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default
    return max(1, min(value, maximum))


def _parse_chat_ids(raw: str) -> list[int]:
    values: list[int] = []
    for item in str(raw or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            logger.warning("Ignoring invalid ALLOWED_CHAT_IDS value %r", item)
    return values


API_ID = os.environ.get("TG_API_ID")
API_HASH = os.environ.get("TG_API_HASH")
ONLY_GROUPS = os.environ.get("ONLY_GROUPS", "1") == "1"
ALLOWED_KEYWORDS = [
    keyword.strip().lower()
    for keyword in os.environ.get("CHAT_KEYWORDS", "").split(",")
    if keyword.strip()
]
ALLOWED_CHAT_IDS = _parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))
TARGET_FOLDER_NAME = os.environ.get("TARGET_FOLDER_NAME", "").strip()
SCAN_HISTORY_LIMIT = _bounded_int_env("SCAN_HISTORY_LIMIT", 50, 500)
PARSE_QUEUE_MAXSIZE = _bounded_int_env("PARSE_QUEUE_MAXSIZE", 200, 2000)
MAX_URLS_PER_MESSAGE = _bounded_int_env("MAX_URLS_PER_MESSAGE", 10, 50)
PERSIST_RETRY_ATTEMPTS = _bounded_int_env("PERSIST_RETRY_ATTEMPTS", 3, 10)
CARD_ALLOWED_USER_IDS = (
    os.environ.get("CARD_ALLOWED_TELEGRAM_USER_IDS")
    or os.environ.get("ALLOWED_TELEGRAM_USER_ID")
    or ""
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data_dir = os.path.join(BASE_DIR, "data")
os.makedirs(data_dir, exist_ok=True)
session_path = os.path.join(data_dir, "userbot.session")


async def notify_admin(client, message: str) -> None:
    """Best-effort alert; the primary data path never depends on it."""
    alert_token = os.environ.get("ALERT_BOT_TOKEN")
    alert_chat_id = os.environ.get("ALERT_CHAT_ID")
    if alert_token and alert_chat_id:
        endpoint = f"https://api.telegram.org/bot{alert_token}/sendMessage"
        payload = {"chat_id": alert_chat_id, "text": f"🚨 ADMIN ALERT:\n{message}"}
        try:
            await asyncio.to_thread(requests.post, endpoint, json=payload, timeout=5)
        except Exception as exc:
            logger.error("Failed to send admin alert via bot: %s", exc)
        return

    try:
        await client.send_message("me", f"🚨 ADMIN ALERT:\n{message}")
    except Exception as exc:
        logger.error("Failed to send admin alert via userbot: %s", exc)


async def _persist_with_retry(operation, description: str) -> bool:
    """Retry a transient persistence failure and make a final failure visible."""
    for attempt in range(1, PERSIST_RETRY_ATTEMPTS + 1):
        try:
            if await operation():
                return True
        except Exception as exc:
            logger.exception("Persistence exception for %s (attempt %s): %s", description, attempt, exc)
        if attempt < PERSIST_RETRY_ATTEMPTS:
            await asyncio.sleep(min(2 ** (attempt - 1), 5))
    logger.error("Persistence failed after %s attempt(s): %s", PERSIST_RETRY_ATTEMPTS, description)
    return False


def is_card_sender_authorized(sender_id, allowed_user_ids: str | None = None) -> bool:
    """Allow /card only for the explicit Telegram-user allow-list."""
    return is_allowed(
        sender_id,
        CARD_ALLOWED_USER_IDS if allowed_user_ids is None else allowed_user_ids,
    )


def is_card_command(text: str) -> bool:
    return bool(text and text.strip().split(maxsplit=1)[0].lower() == "/card")


def _candidate_chat_ids(chat) -> set[int]:
    candidates: set[int] = set()
    raw_id = getattr(chat, "id", None)
    if isinstance(raw_id, int):
        candidates.add(raw_id)
    try:
        marked_peer_id = telethon.utils.get_peer_id(chat)
    except (TypeError, ValueError) as exc:
        logger.debug("Could not derive marked peer ID for chat %r: %s", raw_id, exc)
    else:
        if isinstance(marked_peer_id, int):
            candidates.add(marked_peer_id)
    return candidates


async def is_target_chat(chat) -> bool:
    """Choose a chat only through an explicit ID or title-keyword selector."""
    if ONLY_GROUPS and getattr(chat, "title", None) is None:
        return False

    if ALLOWED_CHAT_IDS:
        return bool(_candidate_chat_ids(chat).intersection(ALLOWED_CHAT_IDS))

    title = (getattr(chat, "title", "") or getattr(chat, "first_name", "") or "").lower()
    if ALLOWED_KEYWORDS:
        return any(keyword in title for keyword in ALLOWED_KEYWORDS)

    # Empty configuration is never equivalent to “all chats”.
    return False


async def resolve_folder_chat_ids(client, folder_name: str) -> set[int] | None:
    """Resolve an exact Telegram folder title to marked peer IDs."""
    try:
        response = await client(GetDialogFiltersRequest())
    except Exception as exc:
        logger.error("Failed to fetch Telegram folders: %s", exc)
        return None

    filters = getattr(response, "filters", response)
    matching_filter = None
    available_titles: list[str] = []
    for dialog_filter in filters:
        title = getattr(dialog_filter, "title", None)
        title_text = title.text if hasattr(title, "text") else str(title) if title else ""
        available_titles.append(title_text or "Unnamed")
        if title_text == folder_name:
            matching_filter = dialog_filter

    logger.info("Available Telegram folders: %s", available_titles)
    if matching_filter is None:
        logger.error("Telegram folder %r was not found", folder_name)
        return None

    peer_ids: set[int] = set()
    for peer in getattr(matching_filter, "include_peers", []) or []:
        try:
            peer_ids.add(telethon.utils.get_peer_id(peer))
        except (TypeError, ValueError) as exc:
            logger.warning("Could not read peer from Telegram folder %r: %s", folder_name, exc)
    return peer_ids


async def handle_card_command(client, sender, text: str) -> None:
    """Generate a card only for an authorised user and always send it by DM."""
    sender_id = getattr(sender, "id", None)
    if not is_card_sender_authorized(sender_id):
        logger.warning("Denied /card command from Telegram user %r", sender_id)
        return

    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await client.send_message(sender_id, "Usage: /card <project name>")
        return

    pdf_path = None
    try:
        from app.card_generator import format_telegram_project_post, generate_pdf_project_card
        import app.airtable_client as airtable_client

        airtable_client.init_cache(force=True)
        match = airtable_client.find_project_by_query(parts[1].strip(), airtable_client.CACHE_PROJECTS)
        if not match:
            await client.send_message(sender_id, "Project not found in the database.")
            return

        project_fields = match.get("fields", {})
        project_id = match["id"]
        units = [
            unit["fields"] for unit in airtable_client.CACHE_UNITS
            if project_id in (unit.get("fields", {}).get("Project Name") or [])
        ]
        await client.send_message(
            sender_id,
            format_telegram_project_post(project_fields, units=units),
        )
        pdf_path = generate_pdf_project_card(project_fields, units=units)
        await client.send_file(sender_id, pdf_path)
    except Exception as exc:
        logger.exception("/card command failed for authorised user %r: %s", sender_id, exc)
        await client.send_message(sender_id, "Could not generate the project card. The error was logged.")
    finally:
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError as exc:
                logger.warning("Could not remove temporary card %s: %s", pdf_path, exc)


async def main():
    """Start only when durable storage and an explicit chat selector are ready."""
    logger.info("Initializing Rise Real Bali Engine")
    client = None
    worker_task = None
    try:
        if not await init_db():
            logger.critical("Postgres initialization failed; listener will not start Telegram processing")
            return
        if not API_ID or not API_HASH:
            logger.critical("TG_API_ID or TG_API_HASH is missing")
            return
        try:
            api_id = int(API_ID)
        except ValueError:
            logger.critical("TG_API_ID must be numeric")
            return

        client = TelegramClient(session_path, api_id, API_HASH)
        await client.start()

        if TARGET_FOLDER_NAME:
            folder_peer_ids = await resolve_folder_chat_ids(client, TARGET_FOLDER_NAME)
            if folder_peer_ids is None:
                logger.critical("Configured Telegram folder could not be resolved; refusing permissive startup")
                return
            for peer_id in folder_peer_ids:
                if peer_id not in ALLOWED_CHAT_IDS:
                    ALLOWED_CHAT_IDS.append(peer_id)
            logger.info("Loaded %s chat(s) from Telegram folder %r", len(folder_peer_ids), TARGET_FOLDER_NAME)

        if not ALLOWED_CHAT_IDS and not ALLOWED_KEYWORDS:
            logger.critical("No ALLOWED_CHAT_IDS, CHAT_KEYWORDS, or resolved folder chats; refusing startup")
            return

        parse_queue: asyncio.Queue[tuple[int, int, str, str]] = asyncio.Queue(
            maxsize=PARSE_QUEUE_MAXSIZE
        )

        async def check_system_health():
            telegram_connected = client.is_connected()
            telegram_roundtrip_ok = False
            if telegram_connected:
                try:
                    telegram_roundtrip_ok = await asyncio.wait_for(client.get_me(), timeout=3.0) is not None
                except Exception as exc:
                    logger.warning("Telegram API health ping failed: %s", exc)

            from app.database import check_db_ping

            database_ping_ok = await check_db_ping()
            configured_selector = bool(ALLOWED_CHAT_IDS or ALLOWED_KEYWORDS)
            details = {
                "telegram_connected": telegram_connected,
                "telegram_roundtrip_ok": telegram_roundtrip_ok,
                "database_ping_ok": database_ping_ok,
                "chat_selector_configured": configured_selector,
                "queue_size": parse_queue.qsize(),
                "queue_capacity": PARSE_QUEUE_MAXSIZE,
            }
            return (
                telegram_connected
                and telegram_roundtrip_ok
                and database_ping_ok
                and configured_selector,
                details,
            )

        async def parser_worker():
            while True:
                message_id, chat_id, chat_title, text = await parse_queue.get()
                try:
                    parsed_data = await parse_message(text, chat_title=chat_title)
                    if parsed_data.get("is_relevant"):
                        projects = parsed_data.get("Projects", {}) or {}
                        project_name = projects.get("Project Name") or "UNKNOWN"
                        persisted = await _persist_with_retry(
                            lambda: save_extraction(
                                message_id=message_id,
                                chat_id=chat_id,
                                project_recid=project_name,
                                object_guess="Parsed via realtime listener",
                                confidence=parsed_data.get("confidence", 0.8),
                                slot="realtime",
                                url_status="none",
                                why=parsed_data.get("reason", ""),
                                needs_human=True,
                                raw_json=parsed_data,
                            ),
                            f"realtime extraction message={message_id} chat={chat_id}",
                        )
                        if not persisted:
                            await notify_admin(client, f"Failed to persist realtime extraction {message_id}/{chat_id}")

                    urls = parsed_data.get("detected_urls", [])
                    if not isinstance(urls, list):
                        urls = []
                    if len(urls) > MAX_URLS_PER_MESSAGE:
                        logger.warning(
                            "URL limit reached for message %s: processing %s of %s",
                            message_id,
                            MAX_URLS_PER_MESSAGE,
                            len(urls),
                        )
                    mirror_project_name = (parsed_data.get("Projects", {}) or {}).get("Project Name")
                    for url in urls[:MAX_URLS_PER_MESSAGE]:
                        if not isinstance(url, str):
                            continue
                        logger.info("Processing detected URL: %s", redact_url(url))
                        link_result = await process_generic_link(
                            url,
                            message_id,
                            chat_id,
                            chat_title=chat_title,
                            project_name=mirror_project_name,
                        )
                        if link_result.get("is_private"):
                            logger.warning("Private link requires access: %s", redact_url(url))
                except Exception as exc:
                    logger.exception("Parser worker failed for message %s/%s: %s", message_id, chat_id, exc)
                    await notify_admin(client, f"Parser worker error for {message_id}/{chat_id}: {exc}")
                finally:
                    parse_queue.task_done()
                    await asyncio.sleep(0.5)

        await start_healthcheck_server(
            port=_bounded_int_env("HEALTHCHECK_PORT", 8080, 65535),
            health_checker=check_system_health,
        )
        asyncio.create_task(
            monitor_health_transitions(
                check_system_health,
                on_unhealthy=lambda details: notify_admin(
                    client, f"Healthcheck unhealthy: {details}"
                ),
                on_recovered=lambda: notify_admin(client, "Healthcheck recovered"),
                interval_seconds=_bounded_int_env(
                    "HEALTH_MONITOR_INTERVAL_SECONDS", 60, 3600
                ),
            ),
            name="listener-health-monitor",
        )
        worker_task = asyncio.create_task(parser_worker(), name="listener-parser-worker")

        @client.on(events.NewMessage)
        async def handle_new_message(event):
            text = event.text or ""
            sender = await event.get_sender()
            # /card is intentionally available by DM response to an authorised
            # sender even if the command was typed in an otherwise ignored chat.
            if is_card_command(text):
                await handle_card_command(client, sender, text)
                return

            chat = await event.get_chat()
            if not await is_target_chat(chat):
                return

            chat_title = getattr(chat, "title", None) or "Private Chat"
            has_media = event.media is not None
            logger.info("Received message %s from selected chat %r", event.id, chat_title)
            persisted = await _persist_with_retry(
                lambda: save_message(
                    event.id,
                    chat.id,
                    getattr(sender, "id", 0),
                    text,
                    has_media,
                ),
                f"raw message={event.id} chat={chat.id}",
            )
            if not persisted:
                await notify_admin(client, f"Failed to persist incoming message {event.id}/{chat.id}; parsing withheld")
                return

            if text.strip() and passes_prefilter(text):
                if parse_queue.full():
                    logger.warning("Parser queue full; applying backpressure to message %s", event.id)
                await parse_queue.put((event.id, chat.id, chat_title, text))

        logger.info("Scanning selected chats (history and bio)")
        target_chats = []
        async for dialog in client.iter_dialogs():
            if await is_target_chat(dialog.entity):
                target_chats.append(dialog)
                logger.info("Selected chat: %r (%s)", dialog.name, dialog.id)

        for dialog in target_chats:
            try:
                await scan_chat_metadata_and_history(
                    client,
                    dialog.entity,
                    limit=SCAN_HISTORY_LIMIT,
                )
            except Exception as exc:
                logger.warning("History scan failed for %r: %s", dialog.name, exc)

        logger.info("Startup scan complete; listening for new messages")
        await client.run_until_disconnected()
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        await close_db()


if __name__ == "__main__":
    from app.single_instance import acquire

    acquire("listener")
    asyncio.run(main())
