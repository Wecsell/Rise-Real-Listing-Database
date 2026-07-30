import pytest
from app.whatsapp_client import (
    format_phone_for_whatsapp_api,
    build_primary_outreach_message,
    send_whatsapp_message,
)

class TestWhatsAppOutreach:
    """Тесты форматирования и генерации сообщений WhatsApp для девелоперов."""

    def test_phone_formatting(self):
        assert format_phone_for_whatsapp_api("08133888995") == "628133888995"
        assert format_phone_for_whatsapp_api("+62 813-3919-882") == "628133919882"
        assert format_phone_for_whatsapp_api("+61 434 639 068") == "61434639068"
        assert format_phone_for_whatsapp_api("invalid") is None

    def test_build_outreach_message(self):
        msg = build_primary_outreach_message("Nuanu Development", "The Wave Villa")
        assert "Nuanu Development" in msg
        assert "The Wave Villa" in msg
        assert "Rise Real" in msg
        assert "Dev Kit" in msg

    def test_simulated_send_message(self):
        res = send_whatsapp_message("08133888995", "Тестовое авто-сообщение")
        assert res["success"] is True
        assert res["chat_id"] == "628133888995@c.us"
