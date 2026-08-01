import unittest
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app.drive_auth as drive_auth


class TestDriveAuth(unittest.TestCase):
    """
    Сервисный аккаунт для зеркала Drive не годится (собственная квота
    хранилища = 0, storageQuotaExceeded даже в расшаренной папке) - нужен
    OAuth-токен от имени владельца, создаваемый один раз вручную через
    tools_drive_auth_setup.py. get_drive_service() сам consent не запускает,
    только читает уже созданный токен.
    """

    def setUp(self):
        drive_auth._service = None  # сброс модульного кэша между тестами

    def test_missing_token_raises_actionable_error(self):
        with patch('app.drive_auth.os.path.exists', return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                drive_auth.get_drive_service()
        self.assertIn("tools_drive_auth_setup.py", str(ctx.exception))

    def test_valid_token_builds_service_without_refresh(self):
        fake_creds = MagicMock(valid=True)
        with patch('app.drive_auth.os.path.exists', return_value=True), \
             patch('app.drive_auth.Credentials.from_authorized_user_file', return_value=fake_creds), \
             patch('app.drive_auth.build', return_value='drive-service') as mock_build:
            service = drive_auth.get_drive_service()

        self.assertEqual(service, 'drive-service')
        mock_build.assert_called_once_with('drive', 'v3', credentials=fake_creds)

    def test_expired_token_with_refresh_token_is_refreshed_and_saved(self):
        fake_creds = MagicMock(valid=False, expired=True, refresh_token='rt')
        fake_creds.to_json.return_value = '{"refreshed": true}'

        with patch('app.drive_auth.os.path.exists', return_value=True), \
             patch('app.drive_auth.Credentials.from_authorized_user_file', return_value=fake_creds), \
             patch('app.drive_auth.build', return_value='drive-service'), \
             patch('builtins.open', unittest.mock.mock_open()) as mock_file:
            drive_auth.get_drive_service()

        fake_creds.refresh.assert_called_once()
        mock_file().write.assert_called_once_with('{"refreshed": true}')

    def test_expired_token_without_refresh_token_raises_actionable_error(self):
        """Без refresh_token тихий сбой недопустим - иначе зеркало годами молча не работает."""
        fake_creds = MagicMock(valid=False, expired=True, refresh_token=None)

        with patch('app.drive_auth.os.path.exists', return_value=True), \
             patch('app.drive_auth.Credentials.from_authorized_user_file', return_value=fake_creds):
            with self.assertRaises(RuntimeError) as ctx:
                drive_auth.get_drive_service()
        self.assertIn("tools_drive_auth_setup.py", str(ctx.exception))

    def test_service_is_cached_across_calls(self):
        fake_creds = MagicMock(valid=True)
        with patch('app.drive_auth.os.path.exists', return_value=True), \
             patch('app.drive_auth.Credentials.from_authorized_user_file', return_value=fake_creds), \
             patch('app.drive_auth.build', return_value='drive-service') as mock_build:
            drive_auth.get_drive_service()
            drive_auth.get_drive_service()

        mock_build.assert_called_once()


if __name__ == '__main__':
    unittest.main()
