"""Regression coverage for untrusted Field Staging attachments."""

from contextlib import asynccontextmanager

import pytest

from app.url_safety import ResponseTooLargeError, UnsafeUrlError


def test_field_staging_table_is_resolved_by_metadata_id(monkeypatch):
    """Field processing must not fall back to Airtable's broken name endpoint."""
    import field_processor

    sentinel = object()
    monkeypatch.setattr(field_processor, "get_table", lambda name: sentinel)
    assert field_processor._staging_table() is sentinel

    monkeypatch.setattr(field_processor, "get_table", lambda name: None)
    with pytest.raises(RuntimeError, match="table ID"):
        field_processor._staging_table()


def test_large_parsed_finding_is_rejected_without_creating_invalid_json(monkeypatch):
    """A slice of JSON cannot be marked processed and promoted later."""
    import field_processor

    monkeypatch.setattr(field_processor, "FIELD_PARSED_JSON_MAX_CHARS", 20)
    with pytest.raises(ValueError, match="exceeds Field Staging capacity"):
        field_processor._serialize_parsed_finding({"Projects": {"notes": "x" * 100}})


@pytest.mark.asyncio
async def test_field_attachment_rejects_untrusted_url_before_fetching():
    """Staging data must not turn the processor into an SSRF client."""
    import field_processor

    with pytest.raises(UnsafeUrlError):
        await field_processor.download_file("http://127.0.0.1:8080/private", ".jpg")


@pytest.mark.asyncio
async def test_field_attachment_stream_is_bounded_and_partial_file_is_removed(monkeypatch):
    import field_processor

    class OversizedResponse:
        status_code = 200
        headers = {}

        async def aiter_bytes(self):
            yield b"too-large"

    @asynccontextmanager
    async def fake_stream(*_args, **_kwargs):
        yield OversizedResponse()

    monkeypatch.setattr(field_processor, "stream_safe_url", fake_stream)
    monkeypatch.setattr(field_processor, "FIELD_ATTACHMENT_MAX_BYTES", 3)

    with pytest.raises(ResponseTooLargeError):
        await field_processor.download_file("https://v5.airtableusercontent.com/file", ".jpg")
