"""A small dependency-free HTTP readiness endpoint for long-running workers."""

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional, Tuple

logger = logging.getLogger("HealthCheck")
START_TIME = time.time()

HealthChecker = Callable[[], Awaitable[Tuple[bool, dict[str, Any]]]]

# The endpoint is probed by Docker on the local container network.  Bound its
# work anyway: a stalled Telegram/DB probe must produce an unhealthy response,
# not accumulate permanently blocked request handlers.
CALLBACK_TIMEOUT_SECONDS = float(os.environ.get("HEALTHCHECK_CALLBACK_TIMEOUT", "5"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("HEALTHCHECK_REQUEST_TIMEOUT", "5"))
MAX_REQUEST_HEADER_BYTES = 16 * 1024

health_check_callback: Optional[HealthChecker] = None


def set_health_checker(callback: Optional[HealthChecker]) -> None:
    global health_check_callback
    health_check_callback = callback


def _response(status: str, body: dict[str, Any], *, include_body: bool = True,
              extra_headers: tuple[bytes, ...] = ()) -> bytes:
    encoded = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    headers = (
        b"HTTP/1.1 " + status.encode("ascii") + b"\r\n"
        b"Content-Type: application/json; charset=utf-8\r\n"
        b"Content-Length: " + str(len(encoded)).encode("ascii") + b"\r\n"
        b"Connection: close\r\n"
        + b"".join(extra_headers)
        + b"\r\n"
    )
    return headers + (encoded if include_body else b"")


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str]:
    """Read only the HTTP headers, with a strict size and time bound."""
    try:
        raw = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=REQUEST_TIMEOUT_SECONDS
        )
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise ValueError("malformed HTTP request") from exc

    if len(raw) > MAX_REQUEST_HEADER_BYTES:
        raise ValueError("HTTP request headers are too large")

    first_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = first_line.split()
    if len(parts) != 3:
        raise ValueError("malformed HTTP request line")

    method, target, _version = parts
    return method.upper(), target.split("?", 1)[0]


async def _check_health() -> tuple[bool, dict[str, Any]]:
    # Snapshot the callback so a test or controlled reconfiguration cannot
    # affect a request that has already started.
    callback = health_check_callback
    if callback is None:
        return True, {}

    try:
        is_healthy, details = await asyncio.wait_for(
            callback(), timeout=CALLBACK_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.error("Health checker timed out after %.1f seconds", CALLBACK_TIMEOUT_SECONDS)
        return False, {"error": "health checker timed out"}
    except Exception:
        # Diagnostics belong in the service logs; exception text can include
        # credentials or upstream URLs and should not be exposed by HTTP.
        logger.exception("Health checker callback failed")
        return False, {"error": "health checker failed"}

    if not isinstance(details, dict):
        logger.error("Health checker returned non-dict details: %r", type(details).__name__)
        return False, {"error": "health checker returned invalid details"}
    return bool(is_healthy), details


async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            method, path = await _read_request(reader)
        except asyncio.TimeoutError:
            response = _response("408 Request Timeout", {"error": "Request Timeout"})
        except ValueError:
            response = _response("400 Bad Request", {"error": "Bad Request"})
        else:
            include_body = method != "HEAD"
            if method not in {"GET", "HEAD"}:
                response = _response(
                    "405 Method Not Allowed",
                    {"error": "Method Not Allowed"},
                    include_body=include_body,
                    extra_headers=(b"Allow: GET, HEAD\r\n",),
                )
            elif path in {"/healthcheck", "/health", "/"}:
                is_healthy, details = await _check_health()
                status_data = {
                    "status": "ok" if is_healthy else "unhealthy",
                    "uptime_seconds": int(time.time() - START_TIME),
                    "details": details,
                }
                response = _response(
                    "200 OK" if is_healthy else "500 Internal Server Error",
                    status_data,
                    include_body=include_body,
                )
            else:
                response = _response(
                    "404 Not Found", {"error": "Not Found"}, include_body=include_body
                )

        writer.write(response)
        await writer.drain()
    except (ConnectionError, OSError) as exc:
        logger.debug("HealthCheck client connection closed: %s", exc)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


async def monitor_health_transitions(
    checker: HealthChecker,
    on_unhealthy: Callable[[dict], Awaitable[None]],
    on_recovered: Optional[Callable[[], Awaitable[None]]] = None,
    interval_seconds: float = 60.0,
    iterations: Optional[int] = None,
) -> None:
    """Poll ``checker`` and alert on state transitions, not on every poll.

    Docker's own HEALTHCHECK only marks the container unhealthy internally -
    nobody is paged unless someone happens to run ``docker ps``. This closes
    that gap without depending on Docker: it runs its own clock, and calls
    ``on_unhealthy``/``on_recovered`` exactly once per transition so a
    prolonged outage doesn't spam the same alert every interval.

    ``iterations`` bounds the loop for tests; production callers leave it
    unset and let the task run until cancelled.
    """
    was_healthy = True
    count = 0
    while iterations is None or count < iterations:
        await asyncio.sleep(interval_seconds)
        count += 1
        try:
            is_healthy, details = await checker()
        except Exception:
            logger.exception("Health monitor: checker raised unexpectedly")
            is_healthy, details = False, {"error": "checker raised"}

        if was_healthy and not is_healthy:
            logger.error("Health check transitioned to UNHEALTHY: %s", details)
            try:
                await on_unhealthy(details)
            except Exception:
                logger.exception("Health monitor: failed to deliver unhealthy alert")
        elif not was_healthy and is_healthy:
            logger.info("Health check recovered")
            if on_recovered is not None:
                try:
                    await on_recovered()
                except Exception:
                    logger.exception("Health monitor: failed to deliver recovery alert")

        was_healthy = is_healthy


async def start_healthcheck_server(
    host: str = "0.0.0.0", port: int = 8080,
    health_checker: Optional[HealthChecker] = None,
) -> asyncio.AbstractServer:
    if health_checker is not None:
        set_health_checker(health_checker)
    server = await asyncio.start_server(
        handle_request, host, port, limit=MAX_REQUEST_HEADER_BYTES
    )
    logger.info("HealthCheck HTTP server running on http://%s:%s/healthcheck", host, port)
    return server
