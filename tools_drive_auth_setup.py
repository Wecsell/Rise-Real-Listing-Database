# -*- coding: utf-8 -*-
"""
Одноразовая настройка OAuth-доступа к личному Google Drive владельца.
Запускать вручную, один раз, в интерактивном терминале с браузером под рукой.

Зачем: implementation_plan.md, Э6 (зеркало рендеров). Сервисный аккаунт для
этого не подходит - у него собственная квота хранилища равна нулю, запись
падает с storageQuotaExceeded даже в папку, расшаренную с личного аккаунта.
Нужен обычный OAuth-токен от имени владельца.

Перед запуском (сделать один раз, руками, в Google Cloud Console):
    1. https://console.cloud.google.com/ -> создать проект (или выбрать существующий).
    2. APIs & Services -> Library -> найти "Google Drive API" -> Enable.
    3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID.
       Application type: Desktop app.
    4. Скачать JSON (кнопка Download) - это client_secret.json.
    5. Положить файл в корень репозитория с именем `gdrive_client_secret.json`
       (уже добавлен в .gitignore - см. ниже) ИЛИ передать путь флагом --secrets.

Запуск:
    python tools_drive_auth_setup.py
    python tools_drive_auth_setup.py --secrets path/to/client_secret.json

Откроется браузер, нужно войти под тем аккаунтом Google, чей Drive будет
использоваться под зеркало, и подтвердить доступ. Результат сохраняется в
data/gdrive_token.json (тоже в .gitignore) - именно его читает app/drive_auth.py.
"""
import os
import sys
import argparse

from dotenv import load_dotenv
load_dotenv()

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
DEFAULT_SECRETS_PATH = "gdrive_client_secret.json"
TOKEN_PATH = os.environ.get("GDRIVE_TOKEN_PATH", "data/gdrive_token.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", default=DEFAULT_SECRETS_PATH,
                    help="путь к client_secret.json, скачанному из Google Cloud Console")
    args = ap.parse_args()

    if not os.path.exists(args.secrets):
        print(f"Не найден файл {args.secrets}.")
        print()
        print("Сначала создай OAuth-клиент в Google Cloud Console:")
        print("  1. https://console.cloud.google.com/ -> создать/выбрать проект")
        print("  2. APIs & Services -> Library -> 'Google Drive API' -> Enable")
        print("  3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID")
        print("     Application type: Desktop app")
        print("  4. Download JSON, положить рядом с этим скриптом как")
        print(f"     '{DEFAULT_SECRETS_PATH}' (или указать --secrets <путь>)")
        sys.exit(1)

    os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(args.secrets, SCOPES)
    print("Открываю браузер для входа и подтверждения доступа...")
    print("Войди под тем аккаунтом Google, чей Drive будет использоваться под зеркало.")
    creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())

    print()
    print(f"Готово. Токен сохранён в {TOKEN_PATH}")
    print("Дальше это читает app/drive_auth.get_drive_service() - руками токен не трогать.")


if __name__ == "__main__":
    main()
