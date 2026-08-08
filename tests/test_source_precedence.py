"""
Source повышается до названия чата, но не понижается обратно.

Смысл поля для агента - "через какой чат у нас есть контакт по этому объекту".
Поэтому найденная связь важнее ручного импорта: если бы Source перезаписывался
слепо, повторный ручной импорт по уже найденному проекту стирал бы связь.
"""
import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.airtable_client import (
    CHANNEL_MANUAL,
    MAX_SOURCE_CHATS,
    SOURCE_SEPARATOR,
    UNKNOWN_SOURCE,
    resolve_source,
)


class TestUpgrade(unittest.TestCase):

    def test_manual_is_upgraded_to_a_chat(self):
        """Основной запрос владельца: сам прислал ссылку, потом проект нашёлся
        в прослушке — Source становится чатом."""
        self.assertEqual(resolve_source(CHANNEL_MANUAL, "TG: PCE Partners"), "TG: PCE Partners")

    def test_manual_is_upgraded_by_whatsapp_too(self):
        self.assertEqual(resolve_source(CHANNEL_MANUAL, "WA: Тимур PCE"), "WA: Тимур PCE")

    def test_legacy_placeholder_is_upgraded(self):
        """~700 записей подписаны заглушкой; настоящий чат её вытесняет,
        а не встаёт рядом."""
        self.assertEqual(resolve_source(UNKNOWN_SOURCE, "TG: PCE Partners"), "TG: PCE Partners")

    def test_url_source_is_upgraded(self):
        """Адрес материала не говорит, через кого он попал к нам."""
        self.assertEqual(
            resolve_source("https://pcepartners.notion.site/?pvs=73", "TG: PCE Partners"),
            "TG: PCE Partners",
        )


class TestNoDowngrade(unittest.TestCase):

    def test_chat_is_not_replaced_by_manual(self):
        """Ручной импорт по найденному проекту не стирает связь."""
        self.assertEqual(resolve_source("TG: PCE Partners", CHANNEL_MANUAL), "TG: PCE Partners")

    def test_chat_is_not_replaced_by_placeholder(self):
        self.assertEqual(resolve_source("TG: PCE Partners", UNKNOWN_SOURCE), "TG: PCE Partners")

    def test_manual_stays_manual_without_a_chat(self):
        self.assertEqual(resolve_source(CHANNEL_MANUAL, CHANNEL_MANUAL), CHANNEL_MANUAL)


class TestSeveralConnects(unittest.TestCase):
    """
    Обычно у проекта один чат: агентство - это мы, в чатах сидят застройщики.
    Второй чат означает, что продажи переехали или проект перепродан другому
    застройщику, поэтому прежний чат не затирается - иначе теряется история.
    """

    def test_second_chat_is_added_in_chronological_order(self):
        """Левее - откуда пришли, правее - где проект сейчас."""
        result = resolve_source("TG: PCE Partners", "TG: PCE Sales 2")
        self.assertEqual(result, f"TG: PCE Partners{SOURCE_SEPARATOR}TG: PCE Sales 2")

    def test_same_chat_twice_does_not_duplicate(self):
        """Прогон по тем же данным не должен менять запись."""
        once = resolve_source("TG: Agency A", "TG: Agency A")
        self.assertEqual(once, "TG: Agency A")
        twice = resolve_source(once, "TG: Agency A")
        self.assertEqual(twice, once)

    def test_accumulation_is_capped(self):
        value = "TG: A"
        for name in ("TG: B", "TG: C", "TG: D", "TG: E"):
            value = resolve_source(value, name)
        self.assertEqual(len(value.split(SOURCE_SEPARATOR)), MAX_SOURCE_CHATS)

    def test_mixed_channels_both_kept(self):
        result = resolve_source("TG: Agency A", "WA: Тимур")
        self.assertIn("TG: Agency A", result)
        self.assertIn("WA: Тимур", result)


class TestEmptyValues(unittest.TestCase):

    def test_empty_existing_takes_incoming(self):
        self.assertEqual(resolve_source(None, "TG: X"), "TG: X")
        self.assertEqual(resolve_source("", "TG: X"), "TG: X")

    def test_empty_incoming_keeps_existing(self):
        self.assertEqual(resolve_source("TG: X", None), "TG: X")
        self.assertEqual(resolve_source("TG: X", "   "), "TG: X")

    def test_both_empty(self):
        self.assertEqual(resolve_source(None, None), "")


class TestWiredIntoUpserts(unittest.TestCase):

    def test_both_upserts_resolve_instead_of_overwriting(self):
        import inspect
        from app import airtable_client
        for fn in (airtable_client.upsert_project, airtable_client.upsert_unit):
            src = inspect.getsource(fn)
            self.assertIn('resolve_source(', src, f"{fn.__name__} перезаписывает Source слепо")
            self.assertNotIn("fields['Source'] = source_label(", src)


class TestMoveIsFlaggedForReview(unittest.TestCase):
    """
    Появление второго чата - не рядовая запись, а сигнал: продажи переехали
    или проект перепродан. Он не должен пройти молча.
    """

    def test_second_chat_is_a_move(self):
        from app.airtable_client import is_source_move
        self.assertTrue(is_source_move("TG: PCE Partners", "TG: PCE Sales 2"))

    def test_first_connect_is_not_a_move(self):
        """Повышение с Manual и вытеснение заглушки - не переезд: связи
        раньше не было, ничего не переезжало."""
        from app.airtable_client import is_source_move
        self.assertFalse(is_source_move(CHANNEL_MANUAL, "TG: PCE Partners"))
        self.assertFalse(is_source_move(UNKNOWN_SOURCE, "TG: PCE Partners"))
        self.assertFalse(is_source_move(None, "TG: PCE Partners"))

    def test_same_chat_is_not_a_move(self):
        """Иначе каждый прогон объявлял бы переезд."""
        from app.airtable_client import is_source_move
        self.assertFalse(is_source_move("TG: PCE Partners", "TG: PCE Partners"))
        self.assertFalse(is_source_move("TG: A" + SOURCE_SEPARATOR + "TG: B", "TG: B"))

    def test_downgrade_is_not_a_move(self):
        from app.airtable_client import is_source_move
        self.assertFalse(is_source_move("TG: PCE Partners", CHANNEL_MANUAL))

    def test_move_goes_through_gaps_not_status(self):
        """Status производный от gaps: прямое присваивание затёрлось бы на
        следующем прогоне, а пометка в gaps держится и объясняет причину."""
        import inspect
        from app import airtable_client
        for fn in (airtable_client.upsert_project, airtable_client.upsert_unit):
            src = inspect.getsource(fn)
            self.assertIn('is_source_move(', src)
            self.assertIn('source_move_gap(', src)
            gap_at = src.index('source_move_gap(')
            status_at = src.index("fields['Status']")
            self.assertLess(gap_at, status_at,
                            f"{fn.__name__}: пометка добавлена после расчёта Status")


if __name__ == '__main__':
    unittest.main()
