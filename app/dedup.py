"""
Поиск повторных находок по номеру телефона с баннера.

Правило: телефон совпал — это дубль, даже если название проекта другое.
Причина в том, как устроен листинг: мы не заносим по одному проекту, а после
получения контакта запрашиваем у застройщика сразу все его проекты. Второй
баннер того же застройщика не дает новой работы — он дает те же материалы.

Координаты в качестве признака сознательно не используются: агент снимает
баннер, а баннер стоит не там, где объект.
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Dedup")

MIN_PHONE_DIGITS = 7


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """
    Приводит номер к сравнимому виду — только цифры, индонезийский 0 в 62.

    Принимает и сырой номер с баннера, и ссылку wa.me, потому что в
    Field Staging контакт уже сохранен как https://wa.me/62...
    """
    if not raw or not isinstance(raw, str):
        return None

    match = re.search(r'wa\.me/(\d+)', raw)
    digits = match.group(1) if match else re.sub(r'\D', '', raw)

    if not digits:
        return None
    if digits.startswith('0'):
        digits = '62' + digits[1:]
    if len(digits) < MIN_PHONE_DIGITS:
        return None
    return digits


def extract_phones(contact_field: Optional[str]) -> List[str]:
    """Все нормализованные номера из строки контактов (их бывает несколько)."""
    if not contact_field or not isinstance(contact_field, str):
        return []

    phones = []
    for part in re.split(r'[,;\n]+', contact_field):
        normalized = normalize_phone(part)
        if normalized and normalized not in phones:
            phones.append(normalized)
    return phones


def find_duplicates(
    phones: List[str],
    records: List[Dict[str, Any]],
    exclude_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Находки с тем же телефоном. Сама проверяемая запись исключается.
    """
    if not phones:
        return []

    wanted = set(phones)
    matches = []
    for rec in records:
        if exclude_id and rec.get('id') == exclude_id:
            continue
        existing = set(extract_phones(rec.get('fields', {}).get('Contact')))
        if existing & wanted:
            matches.append(rec)
    return matches


# Сколько разных проектов на одном номере считать признаком агента.
# У застройщика проектов немного и они его собственные; агент рекламирует
# чужие, поэтому номер быстро обрастает разными названиями.
AGENT_PROJECT_THRESHOLD = 3

SAME_LEAD = 'same_lead'
POSSIBLE_AGENT = 'possible_agent'


def is_known_agency_phone(phones: List[str]) -> bool:
    """
    Числится ли номер за известным агентством.

    Справочник Agencies собран из рабочей воронки партнёров и заведомо неполон:
    в агентстве работает десяток агентов, а номеров записано один-два. Поэтому
    попадание — сильный довод в пользу агента, а отсутствие ничего не доказывает.
    """
    if not phones:
        return False
    try:
        from app.airtable_client import get_agency_phones
        known = get_agency_phones()
    except Exception:
        return False
    return bool(known and set(phones) & known)


def classify_phone_match(matches: List[Dict[str, Any]],
                         incoming_project: Optional[str] = None,
                         phones: Optional[List[str]] = None) -> str:
    """
    Один ли это контакт или похоже на номер агента.

    Совпадение телефона отвечает на вопрос «тот же контакт?», но НЕ на вопрос
    «тот же проект?». У застройщика один номер на все свои проекты — тогда
    повторная находка работы не добавляет. Но баннер мог поставить агент, а
    агент рекламирует объекты разных застройщиков: номер один, проекты разные.
    Случай редкий, однако слить такие записи означало бы потерять проект.

    Два признака агента, в порядке надёжности:
    1. номер числится за известным агентством из справочника Agencies;
    2. на номере накопилось несколько РАЗНЫХ названий проектов.
    """
    if phones and is_known_agency_phone(phones):
        return POSSIBLE_AGENT

    names = set()
    for m in matches:
        name = (m.get('fields', {}).get('Project Name (from Project)') or
                m.get('fields', {}).get('Project Name') or '')
        if isinstance(name, (list, tuple)):
            name = name[0] if name else ''
        name = str(name).strip().lower()
        if name:
            names.add(name)

    if incoming_project:
        incoming = str(incoming_project).strip().lower()
        if incoming:
            names.add(incoming)

    return POSSIBLE_AGENT if len(names) >= AGENT_PROJECT_THRESHOLD else SAME_LEAD


def describe_duplicate(matches: List[Dict[str, Any]], phones: List[str],
                       incoming_project: Optional[str] = None) -> str:
    """Причина срабатывания — для поля Duplicate Reason."""
    if not matches:
        return ""
    ids = ", ".join(str(m.get('fields', {}).get('Id') or m.get('id')) for m in matches[:5])
    base = f"Совпал телефон {', '.join(phones)} с находкой: {ids}"

    if classify_phone_match(matches, incoming_project, phones) == POSSIBLE_AGENT:
        return (f"{base}. ВНИМАНИЕ: на этом номере уже несколько разных проектов — "
                f"похоже на телефон агента, а не застройщика. Объединять записи нельзя, "
                f"проверьте вручную.")
    return base


def build_duplicate_notice(matches: List[Dict[str, Any]], phones: List[str],
                           incoming_project: Optional[str] = None) -> str:
    """Текст уведомления листеру в Telegram."""
    first = matches[0].get('fields', {})
    number = first.get('Id') or matches[0].get('id')
    submitted = first.get('Submitted By')
    when = str(first.get('Submission Time', ''))[:10]

    who = f" от {submitted}" if submitted else ""
    date = f" ({when})" if when else ""

    if classify_phone_match(matches, incoming_project, phones) == POSSIBLE_AGENT:
        text = (
            f"⚠️ Один телефон, разные проекты\n\n"
            f"Номер {phones[0]} уже встречался в находке #{number}{who}{date}, "
            f"но проекты разные."
        )
        text += ("\n\nПохоже, баннеры ставит агент, а не застройщик. Находку "
                 "оставляем как отдельный проект — снимай дальше, это не дубль.")
        return text

    text = (
        f"🔁 Похоже на дубль\n\n"
        f"Телефон {phones[0]} уже есть в находке #{number}{who}{date}."
    )
    if len(matches) > 1:
        text += f"\nВсего совпадений: {len(matches)}."
    text += "\n\nЭто нормально: контакты застройщика мы запрашиваем один раз, а проекты он присылает все сразу. Ехать дальше."
    return text


async def notify_lister(chat_id: Optional[str], text: str) -> bool:
    """
    Отправляет листеру сообщение о дубле.

    Уведомление приходит вдогонку, а не в момент сохранения: телефон
    появляется только после разбора фото, то есть примерно через минуту.
    Листер к этому моменту уже едет дальше, и это нормально.
    """
    token = os.environ.get('TELEGRAM_FIELD_BOT_TOKEN')
    if not token or not chat_id:
        logger.info(f"Уведомление о дубле не отправлено (нет токена или chat_id): {text[:60]}")
        return False

    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление о дубле: {e}")
        return False
