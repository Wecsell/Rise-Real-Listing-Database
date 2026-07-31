"""
Edge case and robustness tests for card_generator and airtable_client lookup.
"""

import sys
import os
import pytest
from app.card_generator import format_telegram_project_post, generate_pdf_project_card
from app.airtable_client import find_project_by_query

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def test_telegram_post_with_markdown_special_chars():
    proj_data = {
        'Project Name': 'VILLA [SPECIAL] _TEST_ *BOLD*',
        'District': 'Canggu & Uluwatu',
        'Developer': 'Dev *Star* & _Co_',
        'Price From (USD)': 150000,
        'Price To (USD)': 250000,
        'Construction stage': 'Structure',
        'Location Link': 'https://google.com/maps?q=1,2',
    }
    post = format_telegram_project_post(proj_data)
    assert 'VILLA' in post
    assert 'Canggu' in post


def test_telegram_post_with_empty_and_none_fields():
    proj_data = {
        'Project Name': None,
        'District': None,
        'Developer': None,
        'Price From (USD)': None,
        'Price To (USD)': None,
    }
    post = format_telegram_project_post(proj_data)
    assert len(post) > 20
    assert 'Проект недвижимости' in post or 'Застройщик' in post or 'Бали' in post


def test_fuzzy_find_project_query_variations():
    mock_projects = [
        {'id': 'rec1', 'fields': {'Project Name': 'CEMAGI ROCK VILLAS', 'Aliases': 'Cemagi Rock'}},
        {'id': 'rec2', 'fields': {'Project Name': 'Lumea Villas', 'Aliases': 'Lumea'}},
        {'id': 'rec3', 'fields': {'Project Name': 'Nuanu Estate'}}
    ]

    # Exact case insensitive
    assert find_project_by_query('cemagi rock villas', mock_projects)['id'] == 'rec1'
    # Alias
    assert find_project_by_query('lumea', mock_projects)['id'] == 'rec2'
    # Substring
    assert find_project_by_query('Cemagi', mock_projects)['id'] == 'rec1'
    # Typo / extra spaces
    assert find_project_by_query('  Cemagi-Rock  ', mock_projects)['id'] == 'rec1'
    # Nonexistent
    assert find_project_by_query('Unknown Nonexistent Project 999', mock_projects) is None


def test_generate_pdf_project_card_with_none_values(tmp_path):
    proj_data = {
        'Project Name': 'Test Empty PDF Project',
        'District': None,
        'Developer': None,
        'Price From (USD)': None,
    }
    pdf_file = str(tmp_path / "empty_test.pdf")
    res = generate_pdf_project_card(proj_data, output_path=pdf_file)
    assert os.path.exists(res)
    assert os.path.getsize(res) > 500
