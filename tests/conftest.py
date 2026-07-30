"""
Общая настройка для тестов.

Переменные окружения читаются модулями `app/*` на уровне импорта, а `load_dotenv()`
вызывают только точки входа (listener, field_processor). Без этого файла тесты,
которым нужен реальный запрос к Airtable, молча помечались бы skipped — то есть
проверка живой схемы никогда бы не выполнялась.

conftest загружается до тестовых модулей, поэтому к моменту импорта app.* токен
уже в окружении.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=False)
except ImportError:  # окружение без python-dotenv — тесты, требующие сети, отметятся skipped
    pass
