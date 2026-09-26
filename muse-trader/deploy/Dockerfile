# MindGod shadow service.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir .

# Fallback config so the image runs standalone; docker-compose mounts the
# real one over it.
COPY deploy/config.week4.yaml /app/config.yaml

ENV MINDGOD_DB_PATH=/data/mindgod.db
VOLUME /data

CMD ["python", "-m", "mindgod", "--config", "/app/config.yaml"]
