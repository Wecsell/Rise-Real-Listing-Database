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
    if len(digits) < MIN_PHONE_DIGITS or len(digits) > 15:
        return None
    return digits


def extract_phones(contact_field: Optional[str]) -> List[str]:
    """Все нормализованные номера из строки контактов (их бывает несколько, в т.ч. слипшиеся через / или |)."""
    if not contact_field or not isinstance(contact_field, str):
        return []

    phones = []
    for part in re.split(r'[,;\n\|]+|(?<=\d|\))\s*/\s*(?=\+|\d|\()|(?<=\d)(?=\+)', contact_field):
        normalized = normalize_phone(part)
        if normalized and normalized not in phones:
            phones.append(normalized)
    return phones


# Домены-площадки: принадлежат не застройщику, а сервису. Совпадение по ним
# ничего не доказывает — на wa.me и в инстаграме сидят все подряд.
PLATFORM_DOMAINS = {
    'wa.me', 'whatsapp.com', 'api.whatsapp.com', 't.me', 'telegram.me',
    'instagram.com', 'facebook.com', 'fb.com', 'linkedin.com', 'youtube.com',
    'tiktok.com', 'google.com', 'drive.google.com', 'docs.google.com',
    'maps.google.com', 'goo.gl', 'bit.ly', 'linktr.ee', 'notion.site',
    'notion.so', 'airtable.com', 'gmail.com', 'mail.ru', 'yandex.ru',
}

_URL_RE = re.compile(r'(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9][a-z0-9\-]*)+)', re.I)
_HANDLE_RE = re.compile(r'@([A-Za-z0-9_]{3,32})')
_TME_RE = re.compile(r'(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,32})', re.I)
_IG_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]{2,40})', re.I)


def extract_websites(text: Optional[str]) -> List[str]:
    """
    Собственные домены из строки контактов, в нижнем регистре и без www.

    Совпадение домена — стопроцентный признак того же застройщика: свой сайт
    не бывает общим, в отличие от телефона, который может принадлежать агенту.
    Домены площадок отбрасываются: они принадлежат сервису, а не компании.
    """
    if not text or not isinstance(text, str):
        return []

    sites = []
    for match in _URL_RE.finditer(text):
        domain = match.group(1).lower().rstrip('.')
        if domain in PLATFORM_DOMAINS:
            continue
        # Отсекаем почтовые адреса: домен уже учтён, а local@ нам не нужен
        if domain not in sites:
            sites.append(domain)
    return sites


def extract_social_handles(text: Optional[str]) -> List[str]:
    """
    Аккаунты в Telegram и Instagram, приведённые к виду 'tg:name' / 'ig:name'.

    Префикс нужен, чтобы @rise в телеграме и @rise в инстаграме не считались
    одним контактом.
    """
    if not text or not isinstance(text, str):
        return []

    handles = []

    def add(value):
        if value and value not in handles:
            handles.append(value)

    for m in _TME_RE.finditer(text):
        add(f'tg:{m.group(1).lower()}')
    for m in _IG_RE.finditer(text):
        add(f'ig:{m.group(1).lower().rstrip(".")}')
    # Голый @ник без указания площадки: в этом проекте это почти всегда Telegram
    for m in _HANDLE_RE.finditer(text):
        add(f'tg:{m.group(1).lower()}')

    return handles




# Сколько разных проектов на одном номере считать признаком агента.
# У застройщика проектов немного и они его собственные; агент рекламирует
# чужие, поэтому номер быстро обрастает разными названиями.
AGENT_PROJECT_THRESHOLD = 3

SAME_LEAD = 'same_lead'
POSSIBLE_AGENT = 'possible_agent'
SAME_COMPANY = 'same_company'


def find_matches(contact_field: Optional[str],
                 records: List[Dict[str, Any]],
                 exclude_id: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    Совпадения по всем видам контакта сразу: сайт, аккаунт, телефон.

    Возвращает словарь по видам, потому что вес у них разный. Сайт решает
    вопрос окончательно — собственный домен не бывает общим. Телефон лишь
    указывает на один контакт: он может принадлежать агенту, который
    рекламирует объекты разных застройщиков.
    """
    sites = set(extract_websites(contact_field))
    handles = set(extract_social_handles(contact_field))
    phones = set(extract_phones(contact_field))

    result = {'website': [], 'handle': [], 'phone': []}
    for rec in records:
        if exclude_id and rec.get('id') == exclude_id:
            continue
        other = rec.get('fields', {}).get('Contact')
        if sites and sites & set(extract_websites(other)):
            result['website'].append(rec)
        elif handles and handles & set(extract_social_handles(other)):
            result['handle'].append(rec)
        elif phones and phones & set(extract_phones(other)):
            result['phone'].append(rec)
    return result


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

    Совпадение по сайту сюда не попадает: оно разбирается раньше и решает
    вопрос само, см. classify_contact_match().
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


def classify_contact_match(matches_by_kind: Dict[str, List[Dict[str, Any]]],
                           incoming_project: Optional[str] = None,
                           phones: Optional[List[str]] = None) -> str:
    """
    Итоговый вывод по всем видам совпадений.

    Порядок жёсткий и отражает разную доказательность:

    1. Сайт — одна и та же компания, без вариантов. Собственный домен не
       делят между собой разные застройщики.
    2. Аккаунт в Telegram или Instagram — почти столь же надёжно.
    3. Телефон — только «тот же контакт». Дальше решает уже проверка на
       агента: у агента один номер на объекты разных застройщиков.
    """
    if matches_by_kind.get('website'):
        return SAME_COMPANY
    if matches_by_kind.get('handle'):
        return SAME_COMPANY
    return classify_phone_match(matches_by_kind.get('phone', []), incoming_project, phones)


def describe_contact_match(matches_by_kind: Dict[str, List[Dict[str, Any]]],
                           contact_field: Optional[str],
                           incoming_project: Optional[str] = None) -> str:
    """Причина срабатывания — для поля Duplicate Reason."""
    def ids(records):
        return ", ".join(str(r.get('fields', {}).get('Id') or r.get('id')) for r in records[:5])

    if matches_by_kind.get('website'):
        sites = ', '.join(extract_websites(contact_field))
        return (f"Совпал сайт {sites} с находкой: {ids(matches_by_kind['website'])}. "
                f"Это тот же застройщик — собственный домен не бывает общим.")

    if matches_by_kind.get('handle'):
        handles = ', '.join(extract_social_handles(contact_field))
        return (f"Совпал аккаунт {handles} с находкой: {ids(matches_by_kind['handle'])}.")

    phone_matches = matches_by_kind.get('phone') or []
    return describe_duplicate(phone_matches, extract_phones(contact_field),
                              incoming_project)


def describe_duplicate(matches: List[Dict[str, Any]], phones: List[str],
                       incoming_project: Optional[str] = None) -> str:
    """Причина срабатывания по телефону — для поля Duplicate Reason."""
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


def build_contact_notice(matches_by_kind: Dict[str, List[Dict[str, Any]]],
                         contact_field: Optional[str],
                         incoming_project: Optional[str] = None) -> str:
    """Текст уведомления листеру с учётом того, ЧЕМ именно совпало."""
    verdict = classify_contact_match(matches_by_kind, incoming_project,
                                     extract_phones(contact_field))

    if verdict == SAME_COMPANY:
        records = matches_by_kind.get('website') or matches_by_kind.get('handle')
        first = records[0].get('fields', {})
        number = first.get('Id') or records[0].get('id')
        what = (', '.join(extract_websites(contact_field))
                if matches_by_kind.get('website')
                else ', '.join(extract_social_handles(contact_field)))
        return (f"🔁 Точно дубль\n\n"
                f"Совпал {what} с находкой #{number}.\n\n"
                f"Это тот же застройщик — собственный сайт и аккаунт не бывают общими. "
                f"Ехать дальше.")

    phone_matches = matches_by_kind.get('phone') or []
    if not phone_matches:
        return ""
    return build_duplicate_notice(phone_matches, extract_phones(contact_field),
                                  incoming_project)


async def notify_lister(chat_id: Optional[str], text: str) -> bool:
    """
    Отправляет листеру сообщение о дубле.

    Уведомление приходит вдогонку, а не в момент сохранения: телефон
    появляется только после разбора фото, то есть примерно через минуту.
    Если менеджер ответил в чате, активна 60-минутная пауза автоответов.
    """
    token = os.environ.get('TELEGRAM_FIELD_BOT_TOKEN')
    if not token or not chat_id:
        logger.info(f"Уведомление о дубле не отправлено (нет токена или chat_id): {text[:60]}")
        return False

    from app.access import is_human_paused
    if is_human_paused(chat_id):
        logger.info(f"Уведомление не отправлено: в чате [{chat_id}] активна 60-мин пауза человека.")
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
