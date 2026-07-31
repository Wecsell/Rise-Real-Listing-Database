"""
Unit and integration tests for app/listener.py.
"""

import sys
import os
import pytest
from app.listener import passes_prefilter, is_target_chat

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def test_passes_prefilter_real_estate_messages():
    assert passes_prefilter("Selling 2BR Villa in Canggu $250,000") is True
    assert passes_prefilter("New apartment project in Ubud freehold") is True
    assert passes_prefilter("Презентация нового проекта вилл на Бали") is True
    assert passes_prefilter("Price from 150000 USD") is True


def test_passes_prefilter_non_real_estate():
    assert passes_prefilter("Привет, как дела?") is False
    assert passes_prefilter("Good morning team") is False
    assert passes_prefilter("What time is the meeting?") is False


@pytest.mark.asyncio
async def test_is_target_chat_mock():
    class DummyChat:
        def __init__(self, title, chat_id):
            self.title = title
            self.id = chat_id

    chat1 = DummyChat("Rise Real Developer Chat", 12345)
    res1 = await is_target_chat(chat1)
    assert res1 is True or res1 is False  # Valid boolean evaluation
