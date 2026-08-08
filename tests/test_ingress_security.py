"""Regression tests for the Telegram and external-link ingress boundary.

Every test is network-isolated.  DNS and HTTP transport are either avoided or
mocked so the suite can run safely in CI and on a developer workstation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import drive_mirror, listener
from app.drive_mirror import mirror_project_drive_files
from app.link_fetcher import process_generic_link
from app.url_safety import UnsafeUrlError, validate_url_origin, validate_public_url


class DummyChat:
    def __init__(self, chat_id=123, title="Rise Real Developers"):
        self.id = chat_id
        self.title = title


@pytest.mark.asyncio
async def test_chat_selection_fails_closed_without_an_explicit_selector(monkeypatch):
    monkeypatch.setattr(listener, "ALLOWED_CHAT_IDS", [])
    monkeypatch.setattr(listener, "ALLOWED_KEYWORDS", [])

    assert await listener.is_target_chat(DummyChat()) is False


def test_card_whitelist_is_explicit_and_denies_by_default():
    assert listener.is_card_sender_authorized(100, allowed_user_ids="100,200") is True
    assert listener.is_card_sender_authorized(300, allowed_user_ids="100,200") is False
    assert listener.is_card_sender_authorized(100, allowed_user_ids="") is False


def test_mirror_requires_an_explicit_root_folder_before_drive_access(monkeypatch):
    # Isolated from the real environment: a deployment with GDRIVE_MIRROR_ROOT_ID
    # configured must still fail closed for a caller that explicitly passes none.
    monkeypatch.setattr(drive_mirror, "ROOT_FOLDER_ID", None)
    with patch("app.drive_mirror.get_drive_service") as get_service:
        with pytest.raises(RuntimeError, match="GDRIVE_MIRROR_ROOT_ID"):
            mirror_project_drive_files("Test project", [], root_id=None)
    get_service.assert_not_called()


@pytest.mark.asyncio
async def test_unsafe_scheme_is_rejected_before_link_download():
    with patch(
        "app.link_fetcher.download_file_from_url", new=AsyncMock()
    ) as download:
        result = await process_generic_link(
            "http://drive.google.com/file/d/unsafe-file/view"
        )

    assert any("security policy" in gap.lower() for gap in result["gaps"])
    download.assert_not_awaited()


def test_allowlist_matching_is_not_a_substring_check():
    with pytest.raises(UnsafeUrlError):
        validate_url_origin("https://notion.site.attacker.invalid/page")


@pytest.mark.asyncio
async def test_private_dns_answer_is_rejected_without_an_http_request(monkeypatch):
    monkeypatch.setattr(
        "app.url_safety.resolve_host_ips",
        AsyncMock(return_value=["127.0.0.1"]),
    )

    with pytest.raises(UnsafeUrlError, match="non-public"):
        await validate_public_url("https://docs.google.com/spreadsheets/d/abc")
