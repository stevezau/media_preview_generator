# The Plex marker agent: the app's database half, to run beside Plex.
#
# Build from the REPOSITORY ROOT (it needs the app's source, which is what it runs):
#   docker build -f plex-marker-agent/Dockerfile -t plex-marker-agent:dev .
#
# It carries no ffmpeg, no GPU drivers and none of the app's media libraries: the app's wheel is installed with
# --no-deps and only the handful of libraries in requirements.txt comes with it.

FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependency wheels first: this layer is keyed on requirements.txt alone, so a source edit never rebuilds them.
COPY plex-marker-agent/requirements.txt ./
RUN pip wheel --wheel-dir=/wheels --no-cache-dir -r requirements.txt

# The app wheel, without its dependencies: the agent imports markers/publishers only (the publisher's one
# import of the server layer is function-local, so none of it loads here).
# setuptools-scm needs a version; CI passes the release's, a local build gets the fallback.
COPY pyproject.toml ./
COPY media_preview_generator/ ./media_preview_generator/
ARG SETUPTOOLS_SCM_PRETEND_VERSION="0.0.0.dev0"
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MEDIA_PREVIEW_GENERATOR="$SETUPTOOLS_SCM_PRETEND_VERSION" \
      pip wheel --wheel-dir=/wheels --no-cache-dir --no-deps .

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="Plex marker agent" \
      org.opencontainers.image.description="Writes intro and credits markers into Plex's database for Media Preview Generator running on another machine" \
      org.opencontainers.image.source="https://github.com/stevezau/media_preview_generator"
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_PORT=9494 \
    PLEX_CONFIG_DIR="/plex/Library/Application Support/Plex Media Server"

COPY --from=builder /wheels /tmp/wheels
# --no-deps: the wheel folder already holds requirements.txt's full closure, and the app wheel's own dependency list
# (ffmpeg-adjacent libraries it never imports here) must not pull anything in.
RUN pip install --no-cache-dir --no-index --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels

WORKDIR /app
COPY plex-marker-agent/plex_marker_agent.py plex-marker-agent/wsgi.py ./

# Non-root by default. Plex's database is owned by the user Plex runs as, so run the container as that user
# (compose: `user: "${PUID}:${PGID}"`); the image itself needs no privileges of its own.
USER 1000:1000
EXPOSE 9494

# One worker: the app's "one writer at a time" guarantee is a lock inside one process (see the module docstring).
# Threads let a read-back and the Edit tab's checks overlap without a second process touching the database.
CMD ["sh", "-c", "exec gunicorn wsgi:app --bind 0.0.0.0:${AGENT_PORT} --workers 1 --threads 4 --timeout 180 --access-logfile -"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD exec python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('AGENT_PORT', '9494') + '/v1/health', timeout=5)"
