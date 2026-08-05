"""Static guards for deployment assumptions that do not need Docker daemon access."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _lines(path: str) -> list[str]:
    return [line.strip() for line in (ROOT / path).read_text(encoding="utf-8").splitlines()]


def test_runtime_image_contains_all_supported_entrypoints():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY app/ ./app/" in dockerfile
    assert "COPY field_bot.py field_processor.py ./" in dockerfile
    assert "COPY requirements.txt ./" in dockerfile
    assert "COPY app/ /usr/src/app/app/" not in dockerfile  # no commented-out runtime copy
    assert "FROM base AS test" in dockerfile
    assert "FROM base AS runtime" in dockerfile
    assert "COPY requirements-dev.txt ./" in dockerfile
    assert "COPY tests/ ./tests/" in dockerfile
    base_stage = dockerfile.split("FROM base AS test", 1)[0]
    runtime_stage = dockerfile.split("FROM base AS runtime", 1)[1]
    assert "requirements-dev.txt" not in base_stage
    assert "COPY tests/" not in base_stage
    assert "requirements-dev.txt" not in runtime_stage
    assert "COPY tests/" not in runtime_stage
    assert dockerfile.rstrip().endswith('CMD ["python", "-u", "-m", "app.listener"]')


def test_compose_waits_for_postgres_and_does_not_mount_source_code():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "condition: service_healthy" in compose
    assert "pg_isready" in compose
    assert "127.0.0.1:8080/healthcheck" in compose
    assert "- ./app:/usr/src/app/app" not in compose
    assert "- ./data:/usr/src/app/data" in compose
    assert "redis:" not in compose
    assert 'profiles: ["field"]' in compose
    assert 'profiles: ["maintenance"]' in compose
    assert 'profiles: ["test"]' in compose
    assert "target: test" in compose
    assert compose.count("target: runtime") == 4
    assert "network_mode: none" in compose
    test_service = compose.split("\n  test:\n", 1)[1]
    assert "env_file:" not in test_service
    assert "PYTEST_ADDOPTS" in test_service


def test_optional_host_postgres_port_is_loopback_only():
    override = (ROOT / "docker-compose.override.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"' in override
    assert "docker-compose.override.yml" not in _lines(".gitignore")


def test_runtime_and_test_dependencies_are_separated():
    runtime = [line for line in _lines("requirements.txt") if line and not line.startswith("#")]
    development = _lines("requirements-dev.txt")

    assert not any(line.startswith("pytest") for line in runtime)
    assert "-r requirements.txt" in development
    assert "pytest==8.3.3" in development
    assert "pytest-asyncio==0.24.0" in development


def test_docker_context_is_allowlisted():
    dockerignore = _lines(".dockerignore")

    assert dockerignore[0].startswith("#")
    assert "*" in dockerignore
    assert "!app/**" in dockerignore
    assert "app/**/*.py[cod]" in dockerignore
    assert "!field_bot.py" in dockerignore
    assert "!tests/**" in dockerignore
    assert "!.env" not in dockerignore
