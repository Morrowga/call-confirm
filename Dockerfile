# Single reusable image for BOTH the FastAPI web process and the Celery
# worker/beat (same codebase, different start command per compose service).
FROM python:3.12-slim

WORKDIR /srv/app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: web. Compose overrides `command:` for worker/beat.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
