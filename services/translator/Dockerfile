# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY ../../requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -r requirements.txt psycopg2-binary

COPY ../../ ./

EXPOSE 8003

CMD ["uvicorn", "services.translator.main:app", "--host", "0.0.0.0", "--port", "8003"]
