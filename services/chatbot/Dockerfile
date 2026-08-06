# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# libreoffice-writer/-calc: DOCX->PDF and XLSX->PDF conversion (Feature H,
# features/converter/) via `soffice --headless`. Only these two
# components are needed (writer_pdf_Export / calc_pdf_Export filters) --
# confirmed in docs/mizan_format_conversion_findings.md -- the full
# `libreoffice` meta-package pulls in Impress/Draw/Base and is unnecessary.
#
# fonts-crosextra-carlito / -caladea + fonts-liberation2: metric-compatible
# substitutes for Calibri/Cambria and Arial/Times New Roman/Courier New.
# Without these, `fc-match Calibri` resolves to DejaVu Sans -- a font with
# different character widths/kerning -- and every DOCX authored in Word's
# default font (nearly all of them) renders with visibly uneven spacing
# after conversion. Confirmed this is the actual cause, not a LibreOffice
# rendering bug, by checking `fc-list`/`fc-match` inside the running
# container before adding these.
#
# fonts-kacst-one: Mizan.ai is Arabic-first, and Word's common Arabic fonts
# (Traditional Arabic, Simplified Arabic) have no installed equivalent
# either -- confirmed `fc-match "Traditional Arabic"` resolves to DejaVu
# Sans's Arabic glyphs, readable but visually a different typeface than the
# source document. There's no metric-compatible open clone of Microsoft's
# Arabic fonts the way Carlito is for Calibri (they're proprietary with no
# open equivalent), but a purpose-built Arabic typeface is a much better
# fallback than generic Latin-first DejaVu Sans. (fonts-noto-naskh-arabic
# doesn't exist as a standalone package on this Debian release -- checked
# via apt-cache search/policy before landing on this package name.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libpq-dev \
        libreoffice-writer libreoffice-calc \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation2 \
        fonts-kacst-one \
    && rm -rf /var/lib/apt/lists/*

COPY ../../requirements.txt ./
# --mount=type=cache persists pip's download/wheel cache across builds (and
# interrupted/retried ones) without baking it into the image -- unlike the
# old PIP_NO_CACHE_DIR=1 approach, a Ctrl-C partway through no longer
# throws away everything already downloaded.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -r requirements.txt psycopg2-binary

COPY ../../ ./

EXPOSE 8000

ENV DB_HOST=mizan-postgres \
    DB_PORT=5432 \
    DB_NAME=mizan_db \
    DB_USER=postgres \
    DB_PASSWORD=postgres123

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
