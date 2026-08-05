"""Fail-closed helpers for fetching user supplied external URLs.

The listener receives URLs from Telegram and from documents authored by third
parties.  This module keeps the network boundary in one place: only HTTPS
hosts on an allow-list are accepted, every redirect is checked again, and DNS
answers that point at non-public addresses are rejected before a request is
made.  Bodies are always consumed with an explicit byte limit.
"""

import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
import urllib.parse
from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterable, List, Optional, Sequence, Set

import httpx


logger = logging.getLogger("UrlSafety")

# The supported link providers.  Additional, explicitly reviewed domains may
# be supplied through EXTERNAL_URL_ALLOWED_HOSTS (comma/space/semicolon
# separated, with optional *.example.com wildcards).
DEFAULT_TRUSTED_HOST_PATTERNS = frozenset({
    "drive.google.com",
    "docs.google.com",
    "accounts.google.com",
    "*.googleusercontent.com",
    "notion.so",
    "*.notion.so",
    "notion.site",
    "*.notion.site",
    "dropbox.com",
    "*.dropbox.com",
    "*.dropboxusercontent.com",
    "yadi.sk",
    "disk.yandex.ru",
    "*.yandex.ru",
})
NOTION_HOST_PATTERNS = frozenset({"notion.so", "*.notion.so", "notion.site", "*.notion.site"})
GOOGLE_HOST_PATTERNS = frozenset({
    "drive.google.com",
    "docs.google.com",
    "accounts.google.com",
    "*.googleusercontent.com",
})

MAX_URL_LENGTH = 4096
DEFAULT_MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """The URL is not safe to access from the service network."""


class ResponseTooLargeError(UnsafeUrlError):
    """The response exceeds the configured streaming limit."""


def _normalise_host(host: str) -> str:
    """Return a lower-case, IDNA normalised hostname or raise ValueError."""
    if not host:
        raise ValueError("missing hostname")
    host = host.rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid hostname") from exc


def _normalise_host_pattern(pattern: str) -> Optional[str]:
    pattern = (pattern or "").strip().lower().rstrip(".")
    wildcard = pattern.startswith("*.")
    raw_host = pattern[2:] if wildcard else pattern
    if not raw_host or "/" in raw_host or ":" in raw_host:
        return None
    try:
        host = _normalise_host(raw_host)
    except ValueError:
        return None
    # Hostnames used in an allow-list must remain hostnames, not arbitrary URL
    # fragments.  Numeric IP literals are never useful here: they are blocked
    # by the public-IP check even if someone accidentally adds one.
    if not all(part and all(c.isalnum() or c == "-" for c in part) for part in host.split(".")):
        return None
    return f"*.{host}" if wildcard else host


def configured_trusted_hosts(raw: Optional[str] = None) -> Set[str]:
    """Return built-in hosts plus explicit deployment-specific additions."""
    if raw is None:
        raw = os.environ.get("EXTERNAL_URL_ALLOWED_HOSTS", "")

    values = str(raw).replace(";", ",").replace(" ", ",").split(",")
    configured = set(DEFAULT_TRUSTED_HOST_PATTERNS)
    for value in values:
        normalised = _normalise_host_pattern(value)
        if normalised:
            configured.add(normalised)
        elif value.strip():
            logger.warning("Ignoring invalid EXTERNAL_URL_ALLOWED_HOSTS entry: %r", value.strip())
    return configured


def host_matches_allowed_pattern(host: str, patterns: Iterable[str]) -> bool:
    """Strictly match a host against exact or ``*.suffix`` allow-list items."""
    try:
        host = _normalise_host(host)
    except ValueError:
        return False

    for candidate in patterns:
        pattern = _normalise_host_pattern(candidate)
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]  # Includes the leading dot.
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == pattern:
            return True
    return False


def validate_url_origin(url: str, allowed_hosts: Optional[Iterable[str]] = None) -> urllib.parse.SplitResult:
    """Validate URL syntax, HTTPS transport and the hostname allow-list.

    This is deliberately synchronous and side-effect free, which makes it
    safe to call before dispatching a potentially expensive task.  DNS checks
    happen in :func:`validate_public_url` immediately before each request.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL is empty")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrlError("URL is too long")

    try:
        parsed = urllib.parse.urlsplit(url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeUrlError("URL is malformed") from exc

    if parsed.scheme.lower() != "https":
        raise UnsafeUrlError("only HTTPS URLs are allowed")
    if not parsed.netloc or not hostname:
        raise UnsafeUrlError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URL credentials are not allowed")
    if port not in (None, 443):
        raise UnsafeUrlError("only the default HTTPS port is allowed")

    try:
        hostname = _normalise_host(hostname)
    except ValueError as exc:
        raise UnsafeUrlError("URL hostname is invalid") from exc

    patterns = set(allowed_hosts) if allowed_hosts is not None else configured_trusted_hosts()
    if not host_matches_allowed_pattern(hostname, patterns):
        raise UnsafeUrlError("URL host is not on the allow-list")
    return parsed


async def resolve_host_ips(host: str, port: int = 443) -> List[str]:
    """Resolve a host once and return unique textual IP addresses."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass

    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise UnsafeUrlError("host could not be resolved") from exc

    addresses: List[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in records:
        address = sockaddr[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UnsafeUrlError("host has no DNS addresses")
    return addresses


def is_public_ip(address: str) -> bool:
    """Allow only globally routable IPv4/IPv6 addresses."""
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


async def validate_public_url(url: str, allowed_hosts: Optional[Iterable[str]] = None) -> urllib.parse.SplitResult:
    """Validate syntax/allow-list and reject any private DNS answer."""
    parsed = validate_url_origin(url, allowed_hosts=allowed_hosts)
    hostname = _normalise_host(parsed.hostname or "")
    addresses = await resolve_host_ips(hostname, parsed.port or 443)
    non_public = [address for address in addresses if not is_public_ip(address)]
    if non_public:
        raise UnsafeUrlError("host resolves to a non-public IP address")
    return parsed


def redact_url(url: str) -> str:
    """Keep logs useful without writing signed query strings or path tokens.

    Telegram's file endpoint puts a bot token in the path, while many storage
    providers place signed credentials in either a path or query string.  The
    host is enough to diagnose a failed fetch, so never preserve either.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return "<invalid-url>"
    host = parsed.hostname or "<missing-host>"
    path = "/…" if parsed.path and parsed.path != "/" else "/"
    suffix = "?…" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{path}{suffix}"


async def _send_with_validated_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str,
    json_body: object,
    headers: Optional[dict],
    timeout: Optional[float],
    allowed_hosts: Optional[Iterable[str]],
    max_redirects: int,
) -> httpx.Response:
    current_url = url
    request_method = method.upper()

    for redirect_count in range(max_redirects + 1):
        await validate_public_url(current_url, allowed_hosts=allowed_hosts)
        request_kwargs = {"headers": headers}
        if json_body is not None:
            request_kwargs["json"] = json_body
        request = client.build_request(request_method, current_url, **request_kwargs)
        response = await client.send(
            request,
            stream=True,
            follow_redirects=False,
        )

        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise UnsafeUrlError("redirect response has no Location header")
            if redirect_count >= max_redirects:
                raise UnsafeUrlError("too many redirects")
            if request_method != "GET":
                raise UnsafeUrlError("redirected non-GET requests are not allowed")
            current_url = urllib.parse.urljoin(current_url, location)
            continue

        return response

    # The loop either returns or raises; this is a defensive guard for type
    # checkers and future edits.
    raise UnsafeUrlError("too many redirects")


@asynccontextmanager
async def stream_safe_url(
    url: str,
    *,
    method: str = "GET",
    json_body: object = None,
    headers: Optional[dict] = None,
    timeout: float = 20.0,
    allowed_hosts: Optional[Iterable[str]] = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    client: Optional[httpx.AsyncClient] = None,
) -> AsyncIterator[httpx.Response]:
    """Yield a streamed response after validating every request hop.

    Redirects are deliberately followed manually instead of using httpx's
    automatic redirect handling so a redirect cannot switch to a private,
    non-HTTPS, or non-allow-listed destination unnoticed.
    """
    if max_redirects < 0:
        raise UnsafeUrlError("max_redirects must not be negative")

    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
    )
    response: Optional[httpx.Response] = None
    try:
        response = await _send_with_validated_redirects(
            http_client,
            url,
            method=method,
            json_body=json_body,
            headers=None if owns_client else headers,
            timeout=timeout,
            allowed_hosts=allowed_hosts,
            max_redirects=max_redirects,
        )
        yield response
    finally:
        if response is not None:
            await response.aclose()
        if owns_client:
            await http_client.aclose()


def _validate_content_length(response: httpx.Response, max_bytes: int) -> None:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    raw_length = response.headers.get("content-length")
    if raw_length is None:
        return
    try:
        content_length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise UnsafeUrlError("invalid Content-Length header") from exc
    if content_length < 0:
        raise UnsafeUrlError("invalid Content-Length header")
    if content_length > max_bytes:
        raise ResponseTooLargeError(f"response is larger than {max_bytes} bytes")


async def read_response_limited(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a small response into memory with a strict streaming cap."""
    _validate_content_length(response, max_bytes)
    chunks: List[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(f"response is larger than {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def stream_response_to_tempfile(
    response: httpx.Response, *, suffix: str, max_bytes: int
) -> str:
    """Stream a bounded response to a temporary file and return its path.

    The partial file is removed before propagating an error, so an oversized
    or interrupted download cannot accumulate on disk.
    """
    _validate_content_length(response, max_bytes)
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = temp_file.name
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLargeError(f"response is larger than {max_bytes} bytes")
            temp_file.write(chunk)
        temp_file.close()
        return path
    except Exception:
        temp_file.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
