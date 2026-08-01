import unittest
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.listener import passes_prefilter, PRE_FILTER_KEYWORDS


class TestPrefilterSubstringLeak(unittest.TestCase):
    """
    PRE_FILTER_KEYWORDS матчится подстрокой ('kw in text_lower'), а не по слову.
    Короткие ключевые слова ('are', 'rp', 'unit') совпадают внутри обычных
    английских слов и пропускают чистый шум в платную очередь Gemini.
    Найдено при разборе economics пайплайна 2026-08-01: 6 из 14 тестовых
    сообщений без единого признака недвижимости проходили фильтр только
    из-за 'are' внутри 'share'/'prepare'/'square' и т.п.
    """

    NOISE_MESSAGES = [
        "Hello, how are you?",           # "are"
        "Thanks, I will share it later", # "sh-ARE"
        "Where are you now?",            # "are"
        "Please prepare the documents",  # prep-ARE
        "We are on our way",             # "are"
        "What is the square footage?",   # squ-ARE (без "of your garden" - геометрия, не недвижимость)
        "Sure, no problem",
        "ok",
        "Happy birthday!!",
        "See you tomorrow",
        "How are you? Are we still on for lunch?",  # "are" отдельным словом, без числа рядом
    ]

    REAL_ESTATE_MESSAGES = [
        "3BR villa in Canggu, freehold, $250k",
        "Новый проект от девелопера, цена от 300000",
        "Leasehold apartment, 2 spal'ni, 120 juta",
        "Land size 6 are, ready to build",  # "are" как единица площади
    ]

    def test_pure_noise_is_not_queued(self):
        leaked = [t for t in self.NOISE_MESSAGES if passes_prefilter(t)]
        self.assertEqual(
            leaked, [],
            f"Чистый шум без признаков недвижимости прошёл фильтр: {leaked}"
        )

    def test_real_estate_messages_still_pass(self):
        blocked = [t for t in self.REAL_ESTATE_MESSAGES if not passes_prefilter(t)]
        self.assertEqual(
            blocked, [],
            f"Настоящие сообщения о недвижимости отфильтрованы: {blocked}"
        )

    def test_short_keywords_do_not_leak_via_substring(self):
        """
        Регрессия на конкретный найденный баг: короткие слова ('are', 'usd', 'rp')
        не должны совпадать как часть более длинного слова, только как отдельное
        слово (регистронезависимо).
        """
        self.assertFalse(passes_prefilter("Thanks, I will share it later"))
        self.assertFalse(passes_prefilter("We used the wrong key"))  # "us-ed", не "usd"
        self.assertFalse(passes_prefilter("This is a sharp corner"))
        self.assertTrue(passes_prefilter("Price is 300000 USD"))
        self.assertTrue(passes_prefilter("Rp 500.000.000"))


if __name__ == '__main__':
    unittest.main()
