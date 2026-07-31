import re
from typing import Optional, Tuple

# Коды стран, встречающиеся в контактах проекта. Список закрытый намеренно:
# по нему определяется, где заканчивается код и начинается номер, а угадывать
# длину кода нельзя — 7 (Россия), 62 (Индонезия) и 971 (ОАЭ) различаются.
# Порядок проверки — от длинных к коротким, иначе 996 съест 9 или 99.
COUNTRY_CODES = {
    '1': 'США/Канада', '7': 'Россия/Казахстан', '20': 'Египет', '27': 'ЮАР',
    '30': 'Греция', '31': 'Нидерланды', '32': 'Бельгия', '33': 'Франция',
    '34': 'Испания', '36': 'Венгрия', '39': 'Италия', '40': 'Румыния',
    '41': 'Швейцария', '43': 'Австрия', '44': 'Великобритания', '45': 'Дания',
    '46': 'Швеция', '47': 'Норвегия', '48': 'Польша', '49': 'Германия',
    '52': 'Мексика', '55': 'Бразилия', '60': 'Малайзия', '61': 'Австралия',
    '62': 'Индонезия', '63': 'Филиппины', '64': 'Новая Зеландия',
    '65': 'Сингапур', '66': 'Таиланд', '81': 'Япония', '82': 'Корея',
    '84': 'Вьетнам', '86': 'Китай', '90': 'Турция', '91': 'Индия',
    '351': 'Португалия', '353': 'Ирландия', '358': 'Финляндия',
    '359': 'Болгария', '370': 'Литва', '371': 'Латвия', '372': 'Эстония',
    '374': 'Армения', '375': 'Беларусь', '380': 'Украина', '385': 'Хорватия',
    '386': 'Словения', '420': 'Чехия', '421': 'Словакия', '852': 'Гонконг',
    '886': 'Тайвань', '965': 'Кувейт', '966': 'Саудовская Аравия',
    '968': 'Оман', '971': 'ОАЭ', '972': 'Израиль', '973': 'Бахрейн',
    '974': 'Катар', '992': 'Таджикистан', '994': 'Азербайджан',
    '995': 'Грузия', '996': 'Киргизия', '998': 'Узбекистан',
}


def split_country_code(digits: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Разделяет номер на код страны и остаток. (None, digits) если код не опознан."""
    if not digits:
        return None, None
    clean = re.sub(r'\D', '', str(digits))
    if not clean:
        return None, None
    for length in (3, 2, 1):
        if clean[:length] in COUNTRY_CODES:
            return clean[:length], clean[length:]
    return None, clean


def format_international(raw: Optional[str]) -> Optional[str]:
    """
    Приводит номер к виду "+62 8133919882" — плюс, код страны, пробел, номер.

    Голая строка цифр вида 6281999599998 читается как абракадабра: непонятно,
    где кончается код страны. Пробел после кода снимает вопрос.

    Если код не опознан, номер возвращается как "+цифры" без разбиения —
    выдумывать границу кода нельзя, ошибка сделает номер нерабочим.
    """
    if not raw:
        return None
    code, rest = split_country_code(raw)
    if code is None:
        clean = re.sub(r'\D', '', str(raw))
        return f"+{clean}" if clean else None
    return f"+{code} {rest}" if rest else f"+{code}"

def format_single_phone(phone_str: str) -> str:
    """
    Нормализует одиночный номер телефона:
    - Заменяет префикс '0' (например 0813...) на '62' (Индонезия).
    - Очищает от пробелов, тире, скобок и плюсов.
    - Возвращает прямую ссылку на WhatsApp: https://wa.me/<digits>
    """
    if not phone_str:
        return ""
        
    s = phone_str.strip()
    
    # Если уже является ссылкой wa.me или whatsapp.com, гарантируем корректный формат
    if "wa.me/" in s or "whatsapp.com" in s:
        # Извлекаем все цифры после wa.me/
        match = re.search(r'wa\.me/(\d+)', s)
        if match:
            digits = match.group(1)
            if digits.startswith("0"):
                digits = "62" + digits[1:]
            return f"https://wa.me/{digits}"
    
    # Очищаем не-цифровые символы, кроме ведущего плюса/нуля
    # Удаляем все пробелы, тире, дефисы, скобки
    cleaned = re.sub(r'[\s\-\(\)\.]', '', s)
    
    # Проверяем старт с 0 (индонезийский локальный формат e.g. 0813...)
    if cleaned.startswith("0"):
        digits = "62" + cleaned[1:]
    elif cleaned.startswith("+"):
        digits = cleaned[1:]
    else:
        digits = cleaned
        
    # Оставляем только цифры
    digits = re.sub(r'\D', '', digits)
    
    # Если получились валидные цифры (не менее 7 цифр для телефона)
    if len(digits) >= 7:
        return f"https://wa.me/{digits}"
    
    return s

def format_whatsapp_link(contact_input: Optional[str]) -> Optional[str]:
    """
    Принимает строку контактов (телефон, список телефонов, соцсети),
    форматирует телефонные номера в кликабельные ссылки WhatsApp
    и гарантирует, что все WhatsApp ссылки идут В ПЕРВУЮ ОЧЕРЕДЬ.
    """
    if not contact_input or not isinstance(contact_input, str):
        return contact_input
        
    s = contact_input.strip()
    if not s:
        return s
        
    parts = re.split(r'[,;\n\|]+|(?<=\d|\))\s*/\s*(?=\+|\d|\()|(?<=\d)(?=\+)', s)
    wa_links = []
    other_contacts = []
    
    for part in parts:
        part_str = part.strip()
        if not part_str:
            continue
            
        has_phone_pattern = re.search(r'(\+?\d[\d\s\-\(\)]{6,}\d)', part_str)
        
        if has_phone_pattern and not part_str.startswith("@") and not "http" in part_str:
            raw_phone = has_phone_pattern.group(1)
            wa_link = format_single_phone(raw_phone)
            formatted = part_str.replace(raw_phone, wa_link)
            if "wa.me/" in formatted:
                wa_links.append(formatted)
            else:
                other_contacts.append(formatted)
        elif "wa.me/" in part_str:
            wa_links.append(format_single_phone(part_str))
        else:
            other_contacts.append(part_str)
            
    # WhatsApp ссылки СТРОГО первыми, остальные контакты следом
    all_ordered = wa_links + other_contacts
    return ", ".join(all_ordered)
