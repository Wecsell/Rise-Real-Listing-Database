import os
import re
import logging
import requests
from typing import Optional, Dict, Any

logger = logging.getLogger("WhatsAppClient")

GREENAPI_INSTANCE_ID = os.environ.get("GREENAPI_INSTANCE_ID", "")
GREENAPI_API_TOKEN = os.environ.get("GREENAPI_API_TOKEN", "")

def format_phone_for_whatsapp_api(phone_str: str) -> Optional[str]:
    """
    Приводит номер телефона к международному формату WhatsApp (без плюсов и нулей в начале).
    Например: '08133888995' -> '628133888995', '+62 813-3919-882' -> '628133919882'.
    """
    if not phone_str:
        return None
        
    match = re.search(r'(\+?\d[\d\s\-\(\)]{6,}\d)', phone_str)
    if not match:
        return None
        
    s = re.sub(r'[\s\-\(\)\.]', '', match.group(1))
    if s.startswith("0"):
        digits = "62" + s[1:]
    elif s.startswith("+"):
        digits = s[1:]
    else:
        digits = s
        
    if len(digits) >= 7:
        return digits
    return None

def build_primary_outreach_message(developer_name: Optional[str] = None, project_name: Optional[str] = None) -> str:
    """
    Формирует вежливое и профессиональное первое авто-сообщение девелоперу.
    """
    greeting = f", {developer_name}" if developer_name else ""
    proj = f" '{project_name}'" if project_name else ""
    
    message = (
        f"Здравствуйте{greeting}! 👋\n\n"
        f"Меня зовут Листер из агентства недвижимости Rise Real на Бали.\n"
        f"Мы заносим ваш проект{proj} в наш первичный каталог для наших покупателей и инвесторов.\n\n"
        f"Подскажите, пожалуйста, могли бы вы прислать актуальную презентацию (Dev Kit) и ссылку на шахматку/наличие юнитов?"
    )
    return message

def send_whatsapp_message(phone: str, text: str) -> Dict[str, Any]:
    """
    Отправляет текстовое сообщение в WhatsApp через Green API.
    """
    formatted_phone = format_phone_for_whatsapp_api(phone)
    if not formatted_phone:
        logger.warning(f"Invalid phone for WhatsApp sending: {phone}")
        return {"success": False, "reason": "Invalid phone format"}
        
    if not GREENAPI_INSTANCE_ID or not GREENAPI_API_TOKEN:
        logger.info(f"[SIMULATION] WhatsApp outreach message to {formatted_phone}: \n{text}")
        return {
            "success": True, 
            "simulated": True, 
            "chat_id": f"{formatted_phone}@c.us", 
            "text": text
        }
        
    url = f"https://api.green-api.com/waInstance{GREENAPI_INSTANCE_ID}/SendMessage/{GREENAPI_API_TOKEN}"
    payload = {
        "chatId": f"{formatted_phone}@c.us",
        "message": text
    }
    
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        return {"success": True, "response": response.json()}
    except Exception as e:
        logger.error(f"Failed to send WhatsApp message to {formatted_phone}: {e}")
        return {"success": False, "error": str(e)}
