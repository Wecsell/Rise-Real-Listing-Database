"""
Тесты для модуля генерации карточек проектов (Telegram Post & PDF).
"""

import os
import pytest
from app.card_generator import format_telegram_project_post, generate_pdf_project_card


def test_format_telegram_project_post_basic():
    proj = {
        'Project Name': 'CEMAGI ROCK VILLAS',
        'District': 'Cemagi',
        'Developer': 'CEMAGI',
        'Construction stage': 'Structure',
        'Price From (USD)': 150000,
        'Price To (USD)': 280000,
        'Lease Term (years)': 30,
        'Distance to the beach, m2': 200,
        'Renders': 'https://drive.google.com/test_renders',
    }
    units = [
        {'Unit type': 'Villa', 'Bedrooms': 1, 'Price from(USD)': 150000},
        {'Unit type': 'Villa', 'Bedrooms': 2, 'Price from(USD)': 280000},
    ]

    post = format_telegram_project_post(proj, units=units)

    assert 'CEMAGI ROCK VILLAS' in post
    assert 'Cemagi' in post
    assert '$150,000 — $280,000' in post
    assert '30 лет' in post
    assert '200 м' in post
    assert 'Villa 1BR' in post
    assert 'https://drive.google.com/test_renders' in post


def test_generate_pdf_project_card_creates_valid_pdf(tmp_path):
    proj = {
        'Project Name': 'BALIBIZA OCEAN VISTA',
        'District': 'Uluwatu',
        'Developer': 'Balibiza',
        'Construction stage': 'Off-plan / Pre-sales',
        'Price From (USD)': 220000,
        'Lease Term (years)': 25,
    }
    units = [
        {'Unit type': 'Apartment', 'Bedrooms': 1, 'Price from(USD)': 220000},
    ]

    pdf_file = str(tmp_path / "test_card.pdf")
    res_path = generate_pdf_project_card(proj, units=units, output_path=pdf_file)

    assert os.path.exists(res_path)
    assert os.path.getsize(res_path) > 1000
