FROM python:3.12-slim AS base

# The application source is copied into the image below.  Deployment must not
# depend on a bind-mounted working tree.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# These are all production entry points. Persistent state (the Telegram
# session and Gemini cache) is supplied at runtime via /usr/src/app/data.
COPY app/ ./app/
COPY field_bot.py field_processor.py ./

# This stage is deliberately separate from the runtime image. It adds the test
# runner and test sources without making either part of a production image.
FROM base AS test

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY tests/ ./tests/
COPY .dockerignore .env.example .gitignore Dockerfile README.md docker-compose.yml docker-compose.override.yml ./

ENV GEMINI_CACHE_DB_PATH=/tmp/gemini_cache.db \
    PYTEST_ADDOPTS="-p no:cacheprovider"

CMD ["pytest", "-q"]

# Keep runtime last so `docker build .` and Compose builds without an explicit
# target always produce the minimal production image.
FROM base AS runtime

CMD ["python", "-u", "-m", "app.listener"]
