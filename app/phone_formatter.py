import re
from typing import Optional

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
