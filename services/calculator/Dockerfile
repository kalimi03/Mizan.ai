# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# build-essential/gcc/libpq-dev: needed to build psycopg2-binary and any
# C-extension deps (this service reads/writes Postgres directly — schema
# init + filing-notes-qa). Kept uniform with services/chatbot/Dockerfile
# even though the specific package that needs this hasn't been isolated —
# see the microservices-split plan's reasoning for keeping this uniform
# across services rather than trimming per-service without verification.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY ../../requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -r requirements.txt psycopg2-binary

COPY ../../ ./

EXPOSE 8002

ENV DB_HOST=mizan-postgres \
    DB_PORT=5432 \
    DB_NAME=mizan_db \
    DB_USER=postgres \
    DB_PASSWORD=postgres123

CMD ["uvicorn", "services.calculator.main:app", "--host", "0.0.0.0", "--port", "8002"]
