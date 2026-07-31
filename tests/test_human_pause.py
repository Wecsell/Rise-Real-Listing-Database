"""
Тесты для 60-минутной паузы автоответов при ответе человека в чате.
"""

import pytest
from app.access import (
    register_human_intervention,
    is_human_paused,
    clear_human_pause,
    get_human_pause_remaining,
)


def test_human_pause_flow():
    chat_id = "12345678"

    # Изначально паузы нет
    clear_human_pause(chat_id)
    assert not is_human_paused(chat_id)
    assert get_human_pause_remaining(chat_id) == 0

    # Человек ответил в чате -> включается пауза
    register_human_intervention(chat_id)
    assert is_human_paused(chat_id)
    assert get_human_pause_remaining(chat_id) > 3500

    # Снятие паузы командой /resume
    clear_human_pause(chat_id)
    assert not is_human_paused(chat_id)
    assert get_human_pause_remaining(chat_id) == 0
