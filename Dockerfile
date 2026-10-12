# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ffmpeg: yt-dlp needs it to merge audio and video for the upload fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't reinstall them. The project itself is "virtual"
# (no build system), so this installs only the locked dependencies.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

COPY bb ./bb

RUN useradd --create-home --uid 1000 bb \
    && mkdir -p /app/data \
    && chown bb:bb /app/data
USER bb
VOLUME /app/data

# SIGINT makes discord.py shut down cleanly (closes the database) on `docker compose stop`.
STOPSIGNAL SIGINT
CMD ["python", "-m", "bb"]
