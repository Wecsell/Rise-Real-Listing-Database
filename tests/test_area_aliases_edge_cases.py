"""
Area normalization and alias edge case tests.
"""

import sys
import os
import pytest
from app.airtable_client import sanitize_area

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def test_sanitize_area_cyrillic_and_variants():
    valid_areas = {'Canggu', 'Uluwatu', 'Ubud', 'Nuanu', 'Seminyak', 'Sanur', 'Bukit'}

    assert sanitize_area("Чангу", valid_areas) == "Canggu"
    assert sanitize_area("чанггу", valid_areas) == "Canggu"
    assert sanitize_area("улувату", valid_areas) == "Uluwatu"
    assert sanitize_area("убуд", valid_areas) == "Ubud"
    assert sanitize_area("нуану", valid_areas) == "Nuanu"
    assert sanitize_area("семиньяк", valid_areas) == "Seminyak"
    assert sanitize_area("санур", valid_areas) == "Sanur"


def test_sanitize_area_unknown():
    valid_areas = {'Canggu', 'Uluwatu', 'Ubud'}
    assert sanitize_area("Неизвестный Район 123", valid_areas) is None
