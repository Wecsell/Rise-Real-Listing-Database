"""
Integration test for /card command flow end-to-end.
Tests: project lookup, telegram post formatting, PDF generation.
"""
import sys
import os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import tempfile
import pytest
import app.airtable_client as ac
from app.card_generator import format_telegram_project_post, generate_pdf_project_card


@pytest.fixture(scope='module')
def cache():
    """Load real Airtable data once for the module."""
    ac.init_cache(force=True)
    return ac.CACHE_PROJECTS, ac.CACHE_UNITS


def test_find_project_cemagi(cache):
    projects, _ = cache
    assert len(projects) > 0, "Cache must have projects loaded"
    res = ac.find_project_by_query('CEMAGI', projects)
    assert res is not None, "Should find CEMAGI ROCK VILLAS"
    assert 'CEMAGI' in res['fields'].get('Project Name', '')


def test_find_project_partial_name(cache):
    projects, _ = cache
    res = ac.find_project_by_query('Lumea', projects)
    assert res is not None, "Should find Lumea Villas by partial match"
    assert 'Lumea' in res['fields'].get('Project Name', '')


def test_find_project_not_found(cache):
    projects, _ = cache
    res = ac.find_project_by_query('NONEXISTENTPROJECT_XYZ', projects)
    assert res is None


def test_telegram_post_from_real_data(cache):
    projects, units = cache
    res = ac.find_project_by_query('CEMAGI', projects)
    assert res is not None

    proj_fields = res['fields']
    proj_id = res['id']
    proj_units = [
        u['fields'] for u in units
        if proj_id in (u.get('fields', {}).get('Project Name') or [])
    ]

    post = format_telegram_project_post(proj_fields, units=proj_units)
    print("\n--- Telegram Post ---")
    print(post[:800])
    assert len(post) > 50
    assert 'CEMAGI' in post


def test_pdf_from_real_data(cache, tmp_path):
    projects, units = cache
    res = ac.find_project_by_query('CEMAGI', projects)
    assert res is not None

    proj_fields = res['fields']
    proj_id = res['id']
    proj_units = [
        u['fields'] for u in units
        if proj_id in (u.get('fields', {}).get('Project Name') or [])
    ]

    pdf_path = str(tmp_path / "cemagi_card.pdf")
    result_path = generate_pdf_project_card(proj_fields, units=proj_units, output_path=pdf_path)
    assert os.path.exists(result_path)
    size = os.path.getsize(result_path)
    print(f"\n--- PDF generated: {result_path} ({size} bytes) ---")
    assert size > 1000, "PDF should have content"
