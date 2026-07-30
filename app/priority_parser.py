from typing import Optional, Dict, Any

VALID_PRIORITIES = ("Высокий", "Средний", "Низкий", "Hight", "High", "Medium", "Low")

# Отрицательные эмоциональные маркеры самого ПРОЕКТА/ОБЪЕКТА (основы слов)
# ПРИМЕЧАНИЕ: Замечания к баннерам, заборам и видимости контакта (выгорел баннер, забор мешает) НЕ являются негативом к проекту!
LOW_KEYWORDS = [
    "не срочн",
    "низкий приоритет",
    "приоритет низкий",
    "черновик",
    "ужасный проект",
    "плохой проект",
    "ужасн",
    "плох",
    "сомнительн",
    "мутн",
    "косячн",
    "не нравит",
    "дорого для эт",
    "зелен",  # зеленая земля, зеленая зона, зеленый зонинг
    "нет документ",  # нет документов
    "без документ",  # без документов
    "проблем с документ",  # проблемы с документами
    "проблемы с документ",  # проблемы с документами
    "нет разрешен",  # нет разрешения
    "без разрешен",  # без разрешения
]

# Положительные эмоциональные маркеры и указания высокого приоритета от ЛИЧНОГО отзыва агента
# ПРИМЕЧАНИЕ: Рекламные слова с баннеров ("роскошный", "дизайнерский", "5 звезд") ИСКЛЮЧЕНЫ, так как это маркетинговые слоганы!
HIGH_KEYWORDS = [
    "срочн",
    "высокий приоритет",
    "приоритет высокий",
    "горяч",
    "красив",
    "топов",
    "супер",
    "бомб",
    "пушк",
    "перспективн",
    "первоклассн",
    "отличн",
    "очень нравит",
]

RISKY_ZONING = ("green", "зеленая", "зелёная")
RISKY_PERMIT_TERMS = ("no permit", "без разреш", "нет pbg")


def has_legal_risk(land_zoning: Optional[str] = None, handover_permits: Optional[str] = None) -> bool:
    """Зеленая зона или отсутствие разрешения на строительство."""
    if land_zoning and str(land_zoning).strip().lower() in RISKY_ZONING:
        return True
    if handover_permits and any(term in str(handover_permits).lower() for term in RISKY_PERMIT_TERMS):
        return True
    return False


def enforce_legal_risk(
    priority: Optional[str],
    land_zoning: Optional[str] = None,
    handover_permits: Optional[str] = None,
) -> str:
    """
    Понижает приоритет до 'Низкий' при юридическом риске, независимо от того,
    что сказал агент голосом и что вернула модель.

    Нужна отдельной функцией, потому что в поле приоритет приходит уже готовым
    из ответа Gemini (по аудио агента), а не считается по ключевым словам.
    Без этой проверки рискованная земля из документов застройщика никак не
    влияла бы на приоритет, если агент не проговорил риск вслух.
    """
    if has_legal_risk(land_zoning, handover_permits):
        return "Низкий"
    return validate_priority(priority)


def parse_priority(text: Optional[str], land_zoning: Optional[str] = None, handover_permits: Optional[str] = None) -> str:
    """
    Извлекает приоритет объекта из текстового комментария на основе ключевых слов и юридических рисков.
    - 'Низкий' выставляется при умышленных рисках (Зеленая зона / Land Zoning Color == "Green", нет документов/разрешения) ИЛИ при негативных ключевых словах в комментарии агента.
    - 'Высокий' выставляется ТОЛЬКО при устной похвале агента или пометках срочно/горячий.
    - 'Средний' во всех остальных случаях по умолчанию.
    """
    # 1. Жесткая привязка юридических рисков (Зеленая земля / Проблемы с разрешениями)
    if has_legal_risk(land_zoning, handover_permits):
        return "Низкий"

    if not text or not isinstance(text, str):
        return "Средний"
    
    text_lower = text.lower()
    
    for kw in LOW_KEYWORDS:
        if kw in text_lower:
            return "Низкий"

    for kw in HIGH_KEYWORDS:
        if kw in text_lower:
            return "Высокий"
            
    return "Средний"

# Реальные значения singleSelect 'Priority' в таблице Field Staging.
# 'Hight' — опечатка в самой Airtable, но это настоящее имя опции,
# поэтому писать нужно именно так.
AIRTABLE_PRIORITY = {
    "высокий": "Hight", "hight": "Hight", "high": "Hight",
    "средний": "Medium", "medium": "Medium",
    "низкий": "Low", "low": "Low",
}
DEFAULT_AIRTABLE_PRIORITY = "Medium"


def to_airtable_priority(priority: Optional[str]) -> str:
    """
    Приводит приоритет к значению, которое реально существует в singleSelect
    'Priority' таблицы Field Staging: 'Hight' / 'Medium' / 'Low'.

    Промпт просит модель возвращать русские значения, а Airtable их не знает и
    отвергает запись. Ошибка уходила в общий except, и находка помечалась
    Status='Error' — то есть каждая находка с русским приоритетом терялась.
    """
    if not isinstance(priority, str):
        return DEFAULT_AIRTABLE_PRIORITY
    return AIRTABLE_PRIORITY.get(priority.strip().lower(), DEFAULT_AIRTABLE_PRIORITY)


def validate_priority(priority: Optional[str]) -> str:
    """
    Проверяет валидность значения приоритета.
    Если значение входит в ('Высокий', 'Средний', 'Низкий'), возвращает его.
    Иначе возвращает 'Средний'.
    """
    if priority in VALID_PRIORITIES:
        return priority
    return "Средний"

from app.phone_formatter import format_whatsapp_link

def build_update_fields(
    priority: Optional[str] = "Средний",
    dev_id: Optional[str] = None,
    final_proj_id: Optional[str] = None,
    contacts: Optional[str] = None,
    final_notes: Optional[str] = None,
    coords: Optional[str] = None,
    status: str = "Processed",
    land_zoning: Optional[str] = None,
    handover_permits: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Формирует словарь update_fields для записи в Airtable,
    исключая ключи со значением None и конвертируя телефоны в WhatsApp ссылки.

    land_zoning / handover_permits прокидываются сюда, чтобы юридический риск
    понижал приоритет в единственной точке реальной записи в Airtable.
    """
    validated_priority = to_airtable_priority(
        enforce_legal_risk(priority, land_zoning, handover_permits)
    )
    formatted_contacts = format_whatsapp_link(contacts) if contacts else None
    
    update_fields = {
        'Status': status,
        'Developer': [dev_id] if dev_id else None,
        'Project': [final_proj_id] if final_proj_id else None,
        'Contact': formatted_contacts,
        'Notes': final_notes,
        'Priority': validated_priority
    }
    
    if coords:
        update_fields['Google Maps Link'] = f"https://www.google.com/maps?q={coords.replace(' ', '')}"
        
    return {k: v for k, v in update_fields.items() if v is not None}
