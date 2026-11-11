# The Celery worker (docker-compose.yml, service "worker"): processes upload jobs when
# INGESTION_MODE=queue. The API itself runs outside Docker (uvicorn) locally and on Vercel.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/backend

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .

# Not root. The number of processes comes from WORKER_CONCURRENCY (app/worker.py).
RUN useradd --create-home worker
USER worker

# Production defaults: no gossip, mingle or heartbeat, and no task events (-E). They send pub/sub
# traffic over Redis that a metered Redis bills for and that only Flower uses; docker-compose.yml
# starts the local worker with -E for Flower.
CMD ["celery", "-A", "app.worker", "worker", "--loglevel=INFO", "-Q", "documind",      "--without-gossip", "--without-mingle", "--without-heartbeat"]
