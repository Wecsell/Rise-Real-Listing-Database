"""
OAuth-авторизация от имени владельца для зеркалирования рендеров на его
личном Google Drive (implementation_plan.md, Э6).

Почему не сервисный аккаунт: у сервисного аккаунта собственная квота
хранилища равна нулю, любая запись падает с storageQuotaExceeded, даже в
папку, расшаренную с личного аккаунта владельца - его 5 ТБ ему недоступны
(проверено 2026-08-01, см. implementation_plan.md). Нужен обычный OAuth
consent от владельца, разово, через tools_drive_auth_setup.py.
"""
import os
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger("DriveAuth")

SCOPES = ["https://www.googleapis.com/auth/drive"]

TOKEN_PATH = os.environ.get("GDRIVE_TOKEN_PATH", "data/gdrive_token.json")

_service = None


def get_drive_service():
    """
    Возвращает authenticated Drive API v3 client от имени владельца.
    Токен должен быть уже создан через tools_drive_auth_setup.py - эта
    функция сама consent-флоу не запускает (для этого нужен интерактивный
    браузер владельца, а не фоновый процесс бота).
    """
    global _service
    if _service is not None:
        return _service

    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"Токен Google Drive не найден по пути {TOKEN_PATH}. "
            f"Запусти `python tools_drive_auth_setup.py` один раз вручную "
            f"и подтверди доступ в браузере."
        )

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
        else:
            raise RuntimeError(
                f"Токен в {TOKEN_PATH} невалиден и не может быть обновлён "
                f"(нет refresh_token). Перезапусти tools_drive_auth_setup.py."
            )

    _service = build("drive", "v3", credentials=creds)
    return _service
