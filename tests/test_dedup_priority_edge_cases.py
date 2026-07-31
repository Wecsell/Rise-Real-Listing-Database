"""
Edge case tests for dedup.py, priority_parser.py, and phone_formatter.py.
"""

import sys
import os
import pytest
from app.dedup import extract_phones, find_matches, classify_contact_match
from app.priority_parser import build_update_fields, parse_priority
from app.phone_formatter import format_international

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def test_extract_phones_multiple_formats():
    raw_contacts = "https://wa.me/6281234567890, https://wa.me/6281999887766; +62(361)123456"
    phones = extract_phones(raw_contacts)
    assert len(phones) == 3
    assert "6281234567890" in phones
    assert "6281999887766" in phones


def test_format_international_phone_variations():
    assert format_international("081234567890") == "+62 81234567890" or format_international("081234567890") == "+081234567890"
    assert format_international("+62 812-3456-7890") == "+62 81234567890"
    assert format_international("6281234567890") == "+62 81234567890"


def test_priority_conflict_resolution():
    # Negative override (green land / legal issues) should downgrade priority to Low even if praised
    audio_text = "Проект очень классный, супер виллы, но тут зеленая зона и нет документов"
    prio = parse_priority(audio_text)
    assert prio == "Низкий"


def test_build_update_fields_robustness():
    res = build_update_fields(
        priority="Высокий",
        contacts=None,
        final_notes=None,
        coords=None,
        status="Processed"
    )
    assert res['Status'] == 'Processed'
    assert 'Priority' in res
