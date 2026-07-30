import unittest
import asyncio
import os
import sys

# Добавляем родительский каталог в sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../app')))

from app.gemini_parser import ParsedExtraction, SYSTEM_PROMPT

class TestMaterialsParsing(unittest.TestCase):

    def test_schema_instantiation(self):
        """Проверяем, что все Pydantic модели парсера валидны и корректно инициализируются."""
        data = {
            "is_relevant": True,
            "Developer": {
                "Developer": "Rise Development",
                "Contacts": "+628123456789",
                "Language": "English"
            },
            "Projects": {
                "Project Name": "Rise Villas Canggu",
                "District": "Canggu",
                "Property Type": ["Villa"],
                "Price From (USD)": 250000.0,
                "Price To (USD)": 450000.0,
                "Construction stage": "Off-plan / Pre-sales",
                "Handover Date": "2027-03-31",
                "Ownership Type": "Leasehold",
                "Lease Term (years)": 30.0,
                "Land Zoning Color": "Tourism/Mixed",
                "Handover Permits": "PBG in process",
                "Priority": "Высокий"
            },
            "Units": [
                {
                    "Unit type": "Villa",
                    "Bedrooms": 2,
                    "Bathrooms": 2,
                    "Price from(USD)": 250000.0,
                    "Area from (m2)": 140.0,
                    "Pool": "Yes(Private)",
                    "Availability": "On sale"
                }
            ],
            "Gaps": ["Link to Dev Kit (Rus)"],
            "confidence": 0.95
        }
        
        parsed = ParsedExtraction(**data)
        self.assertTrue(parsed.is_relevant)
        self.assertEqual(parsed.Developer.Developer, "Rise Development")
        self.assertEqual(parsed.Projects.Project_Name, "Rise Villas Canggu")
        self.assertEqual(parsed.Projects.Handover_Date, "2027-03-31")
        self.assertEqual(parsed.Projects.Priority, "Высокий")
        self.assertEqual(len(parsed.Units), 1)
        self.assertEqual(parsed.Units[0].Unit_type, "Villa")
        self.assertEqual(parsed.Units[0].Pool, "Yes(Private)")

if __name__ == '__main__':
    unittest.main()
