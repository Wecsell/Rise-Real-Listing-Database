import asyncio
import json
import pytest
import pytest_asyncio
import urllib.request
import urllib.error
import app.healthcheck as healthcheck
from app.healthcheck import start_healthcheck_server, set_health_checker

@pytest.fixture(autouse=True)
def reset_checker():
    set_health_checker(None)

@pytest_asyncio.fixture
async def server():
    srv = await start_healthcheck_server(host="127.0.0.1", port=0)
    port = srv.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.close()
    await srv.wait_closed()


async def fetch(url: str):
    """
    Делает HTTP-запрос из отдельного потока и возвращает (status_code, body_dict).

    urlopen блокирующий. Если вызвать его прямо в корутине теста, он заблокирует
    тот самый event loop, на котором работает healthcheck-сервер: обработчик
    никогда не получит управление и не ответит, а тест повиснет навсегда.
    Поэтому запрос уходит в thread pool. По той же причине тело ответа читается
    внутри потока — включая тело ошибки.
    """
    def _do():
        try:
            with urllib.request.urlopen(urllib.request.Request(url)) as resp:
                return resp.status, resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8')

    status, raw = await asyncio.to_thread(_do)
    return status, json.loads(raw)


@pytest.mark.asyncio
async def test_healthcheck_healthy(server):
    async def mock_checker():
        return True, {"telegram_connected": True, "telegram_roundtrip_ok": True, "database_ping_ok": True, "queue_size": 0}

    set_health_checker(mock_checker)

    status, data = await fetch(f"{server}/healthcheck")
    assert status == 200
    assert data["status"] == "ok"
    assert data["details"]["telegram_roundtrip_ok"] is True
    assert data["details"]["database_ping_ok"] is True

@pytest.mark.asyncio
async def test_healthcheck_db_down_500(server):
    """Проверяет, что при сбое БД (database_ping_ok = False) сервер возвращает 500 Internal Server Error."""
    async def mock_checker():
        # Баг-фикс: db_ok обязателен! При отвале БД должен возвращаться статус 500.
        return False, {"telegram_connected": True, "telegram_roundtrip_ok": True, "database_ping_ok": False, "queue_size": 0}

    set_health_checker(mock_checker)

    status, body = await fetch(f"{server}/healthcheck")
    assert status == 500
    assert body["status"] == "unhealthy"
    assert body["details"]["database_ping_ok"] is False

@pytest.mark.asyncio
async def test_healthcheck_unhealthy_500(server):
    """Проверяет, что при сбое Telegram (telegram_roundtrip_ok = False) сервер возвращает 500 Internal Server Error."""
    async def mock_checker():
        return False, {"telegram_connected": True, "telegram_roundtrip_ok": False, "database_ping_ok": True, "queue_size": 5}

    set_health_checker(mock_checker)

    status, body = await fetch(f"{server}/healthcheck")
    assert status == 500
    assert body["status"] == "unhealthy"
    assert body["details"]["telegram_roundtrip_ok"] is False

@pytest.mark.asyncio
async def test_healthcheck_exception_500(server):
    async def mock_checker():
        raise ConnectionError("Telegram RPC call failed")

    set_health_checker(mock_checker)

    status, body = await fetch(f"{server}/healthcheck")
    assert status == 500
    assert body["status"] == "unhealthy"
    # Details remain safe to expose; the full exception is in service logs.
    assert body["details"]["error"] == "health checker failed"


@pytest.mark.asyncio
async def test_healthcheck_callback_timeout_is_unhealthy(server, monkeypatch):
    monkeypatch.setattr(healthcheck, "CALLBACK_TIMEOUT_SECONDS", 0.01)

    async def slow_checker():
        await asyncio.sleep(1)
        return True, {}

    set_health_checker(slow_checker)

    status, body = await fetch(f"{server}/healthcheck")
    assert status == 500
    assert body["status"] == "unhealthy"
    assert body["details"]["error"] == "health checker timed out"

@pytest.mark.asyncio
async def test_healthcheck_not_found(server):
    status, _ = await fetch(f"{server}/invalid_route")
    assert status == 404

@pytest.mark.asyncio
async def test_healthcheck_concurrency_under_load(server):
    """Проверяет отзывчивость сервера при выполнении нескольких тяжелых CPU задач в asyncio.to_thread."""
    async def mock_checker():
        return True, {"telegram_roundtrip_ok": True, "database_ping_ok": True}

    set_health_checker(mock_checker)

    def heavy_cpu_task():
        # Настоящая синхронная CPU-работа (тяжелые вычисления), вынесенная в thread pool
        total = 0
        for i in range(1_000_000):
            total += i
        return total

    # Запускаем 3 параллельные тяжелые вычислительные задачи в отдельных потоках
    cpu_tasks = [asyncio.to_thread(heavy_cpu_task) for _ in range(3)]

    # Выполняем 10 параллельных запросов к healthcheck во время CPU работы
    results = await asyncio.gather(*[fetch(f"{server}/healthcheck") for _ in range(10)], *cpu_tasks)

    for status, data in results[:10]:
        assert status == 200
        assert data["status"] == "ok"
