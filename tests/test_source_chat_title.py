"""
Запись помнит, из какого чата пришла.

Регресс: Source был константой "TG: Rise Real Bali Chat" на КАЖДОЙ записи —
456 юнитов и 244 проекта утверждали один и тот же источник независимо от
группы. Из-за этого Units."Group with agency" нельзя было заполнить даже
задним числом: происхождение записи нигде не сохранялось.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.airtable_client import UNKNOWN_SOURCE, source_label


class TestSourceLabel(unittest.TestCase):

    def test_real_chat_title_is_used(self):
        self.assertEqual(source_label("PCE Partners"), "TG: PCE Partners")

    def test_whatsapp_channel(self):
        from app.airtable_client import CHANNEL_WA
        self.assertEqual(source_label("Тимур (PCE)", CHANNEL_WA), "WA: Тимур (PCE)")

    def test_manual_carries_no_name(self):
        """Ссылку прислал владелец — имя человека в базе не хранится."""
        from app.airtable_client import CHANNEL_MANUAL
        self.assertEqual(source_label(None, CHANNEL_MANUAL), "Manual")
        self.assertEqual(source_label("Mikhail", CHANNEL_MANUAL), "Manual")

    def test_channels_are_only_three(self):
        """Notion/сайт каналами не являются: парсер сам туда не попадает,
        ссылка приходит либо от владельца, либо из прослушки."""
        from app.airtable_client import KNOWN_CHANNELS
        self.assertEqual(set(KNOWN_CHANNELS), {"TG", "WA", "Manual"})

    def test_unknown_channel_falls_back_to_telegram(self):
        self.assertEqual(source_label("X", "Notion"), "TG: X")

    def test_same_label_covers_groups_and_private_chats(self):
        """В папке Telegram лежат и группы, и личные чаты — подпись не должна
        утверждать «группа», она называет сам чат."""
        self.assertEqual(source_label("Tim PCE"), "TG: Tim PCE")

    def test_missing_title_falls_back_to_placeholder(self):
        for empty in (None, "", "   "):
            self.assertEqual(source_label(empty), UNKNOWN_SOURCE)

    def test_placeholder_collides_with_a_real_chat_of_that_name(self):
        """
        Известное ограничение: заглушка совпадает с подписью реального чата
        "Rise Real Bali Chat", поэтому по Source нельзя отличить "источник не
        записан" от "пришло из этой группы".

        Строку намеренно НЕ меняем: Source перезаписывается при каждом upsert,
        и новое значение заглушки молча переписало бы источник у ~700 уже
        существующих записей. Различать источник надо по Units."Group with
        agency", которое заполняется только при известном чате.
        """
        self.assertEqual(source_label("Rise Real Bali Chat"), UNKNOWN_SOURCE)


class TestChatTitleReachesTheWrite(unittest.TestCase):
    """
    Проверяем сам провод: параметр должен быть в сигнатуре и доезжать из
    payload через sync_job, иначе listener пишет chat_title в пустоту.
    """

    def test_upsert_signatures_accept_chat_title(self):
        import inspect
        from app import airtable_client
        for fn in (airtable_client.upsert_project, airtable_client.upsert_unit):
            params = inspect.signature(fn).parameters
            self.assertIn('chat_title', params, f"{fn.__name__} не принимает chat_title")

    def test_sync_job_passes_chat_title_to_both_upserts(self):
        import inspect
        from app import sync_job
        src = inspect.getsource(sync_job._sync_payload)
        self.assertIn('payload.get("chat_title")', src)
        self.assertEqual(src.count('chat_title=chat_title'), 2,
                         "chat_title должен уходить и в проект, и в юнит")

    def test_listener_puts_chat_title_into_payload(self):
        """Без этого payload доезжает до sync_job без источника."""
        source = open(os.path.join(os.path.dirname(__file__), '..', 'app', 'listener.py'),
                      encoding='utf-8').read()
        self.assertIn('parsed_data["chat_title"] = chat_title', source)


class TestGroupWithAgencyComesFromSource(unittest.TestCase):
    """
    С 06.08.2026 Units.'Group with agency' — lookup на Source, то есть группа
    подтягивается сама. Коду достаточно правильно поставить Source; попытка
    записать сам lookup уронила бы юнит с 422.
    """

    def test_field_is_a_lookup_in_the_live_base(self):
        from app.airtable_client import get_field_type
        for table in ('Units', 'Units (Secondary)'):
            ftype = get_field_type(table, 'Group with agency')
            if ftype is None:
                self.skipTest('схема Airtable недоступна')
            self.assertEqual(ftype, 'multipleLookupValues',
                             f'{table}: ожидался lookup на Source')

    def test_code_does_not_write_it(self):
        import inspect
        from app import airtable_client
        src = inspect.getsource(airtable_client.upsert_unit)
        self.assertNotIn("fields['Group with agency']", src)


if __name__ == '__main__':
    unittest.main()
