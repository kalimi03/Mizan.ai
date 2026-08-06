# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# libreoffice-writer/-calc + fonts: DOCX->PDF and XLSX->PDF conversion
# (Feature H, features/converter/) via `soffice --headless`. Same
# package set/rationale as the original services/chatbot/Dockerfile, moved
# here since this is the one service that actually runs LibreOffice now.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libpq-dev \
        libreoffice-writer libreoffice-calc \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation2 \
        fonts-kacst-one \
    && rm -rf /var/lib/apt/lists/*

COPY ../../requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -r requirements.txt psycopg2-binary

COPY ../../ ./

EXPOSE 8004

CMD ["uvicorn", "services.converter.main:app", "--host", "0.0.0.0", "--port", "8004"]
