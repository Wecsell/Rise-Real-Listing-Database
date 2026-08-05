"""Offline checks for fail-closed Airtable table resolution."""

from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch

import app.airtable_client as ac


class _FakeBase:
    def __init__(self):
        self.requested_ids = []

    def table(self, table_id):
        self.requested_ids.append(table_id)
        return ("table", table_id)


class TestAirtableTableResolver(unittest.TestCase):
    def setUp(self):
        self._schema_options = ac._SCHEMA_OPTIONS
        self._table_ids = dict(ac._TABLE_IDS)
        self._schema_fields = {name: set(fields) for name, fields in ac._SCHEMA_FIELDS.items()}
        self._schema_field_types = dict(ac._SCHEMA_FIELD_TYPES)
        ac._SCHEMA_OPTIONS = None
        ac._TABLE_IDS.clear()
        ac._SCHEMA_FIELDS.clear()
        ac._SCHEMA_FIELD_TYPES.clear()

    def tearDown(self):
        ac._SCHEMA_OPTIONS = self._schema_options
        ac._TABLE_IDS.clear()
        ac._TABLE_IDS.update(self._table_ids)
        ac._SCHEMA_FIELDS.clear()
        ac._SCHEMA_FIELDS.update(self._schema_fields)
        ac._SCHEMA_FIELD_TYPES.clear()
        ac._SCHEMA_FIELD_TYPES.update(self._schema_field_types)

    def test_metadata_loader_maps_display_names_to_table_ids(self):
        metadata = {
            "tables": [
                {
                    "id": "tblProjects123",
                    "name": "Projects",
                    "fields": [
                        {
                            "name": "District",
                            "type": "singleSelect",
                            "options": {"choices": [{"name": "Ubud"}]},
                        }
                    ],
                },
                {"id": "tblUnits456", "name": "Units", "fields": []},
            ]
        }
        response = MagicMock()
        response.__enter__.return_value = response

        with (
            patch.object(ac, "AIRTABLE_TOKEN", "test-token"),
            patch.object(ac, "AIRTABLE_BASE_ID", "appTest"),
            patch("urllib.request.urlopen", return_value=response),
            patch("json.load", return_value=metadata),
        ):
            options = ac._load_schema_options()

        self.assertEqual(ac._TABLE_IDS, {"Projects": "tblProjects123", "Units": "tblUnits456"})
        self.assertEqual(options[("Projects", "District")], ["Ubud"])

    def test_selects_fields_and_table_ids_share_one_initializer(self):
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            ac._TABLE_IDS["Projects"] = "tblProjects123"
            ac._SCHEMA_FIELDS["Projects"] = {"District"}
            return {("Projects", "District"): ["Ubud"]}

        with patch.object(ac, "_load_schema_options", side_effect=loader):
            self.assertEqual(ac.get_table_id("Projects"), "tblProjects123")
            self.assertEqual(ac.get_select_options("Projects", "District"), ["Ubud"])
            self.assertTrue(ac.field_exists("Projects", "District"))

        self.assertEqual(calls, 1)

    def test_table_requests_use_metadata_id_not_display_name(self):
        base = _FakeBase()
        ac._SCHEMA_OPTIONS = {}
        ac._TABLE_IDS["Projects"] = "tblProjects123"

        with patch.object(ac, "get_base", return_value=base):
            table = ac.get_table("Projects")

        self.assertEqual(table, ("table", "tblProjects123"))
        self.assertEqual(base.requested_ids, ["tblProjects123"])

    def test_unknown_display_name_fails_closed_without_table_request(self):
        base = _FakeBase()
        ac._SCHEMA_OPTIONS = {}

        with patch.object(ac, "get_base", return_value=base):
            self.assertIsNone(ac.get_table("Projects"))

        self.assertEqual(base.requested_ids, [])

    def test_application_code_has_no_name_based_table_calls(self):
        app_dir = Path(__file__).resolve().parents[1] / "app"
        literal_table_call = re.compile(r"\.table\s*\(\s*['\"]")
        offenders = [
            path.relative_to(app_dir).as_posix()
            for path in app_dir.rglob("*.py")
            if literal_table_call.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])
