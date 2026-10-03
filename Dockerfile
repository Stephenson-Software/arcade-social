FROM python:3.12-slim

# arcade-social: sign-in, scores, achievements and likes for arcade games.
# Standard library only (sqlite3 included) - there is nothing to pip install.
WORKDIR /app
COPY src/ ./src/

RUN useradd --system --no-create-home social \
    && mkdir -p /data /config/arcade /config/play && chown social /data

USER social

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    ARCADE_SOCIAL_HOST=0.0.0.0 \
    ARCADE_SOCIAL_PORT=8080 \
    ARCADE_SOCIAL_DB=/data/arcade-social.sqlite3 \
    ARCADE_SOCIAL_REGISTRY=/config/arcade/games.yaml \
    ARCADE_SOCIAL_BOARDS=/config/play/boards.yaml

# /data holds the SQLite database: player data, so it MUST be backed up
# (python -m arcade_social backup FILE). /config/arcade and /config/play are
# the gateway's config directories, mounted read-only.
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]

CMD ["python3", "-m", "arcade_social", "serve"]
