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

# -E sends the task events Flower shows. Not root. One process per CPU is Celery's default; two is enough locally.
RUN useradd --create-home worker
USER worker

CMD ["celery", "-A", "app.worker", "worker", "--loglevel=INFO", "--concurrency=2", "-Q", "documind", "-E"]
