# syntax=docker/dockerfile:1.4
#
# Feature E document extraction -- separate always-on service, not bundled
# into services/chatbot/Dockerfile. Keeps Docling's dependency chain
# (pandas, opencv system libs, some CPU torch build pulled in transitively
# by docling_ibm_models' table-structure model) isolated from the main
# gateway image, same reasoning as why features/explainer/offline/ is
# kept separate -- except this one needs to be an always-on HTTP service
# (interactive upload-and-review flow), not a manual batch job, so unlike
# that pipeline this DOES get a docker-compose entry.
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# libgl1/libglib2.0-0/libsm6/libxext6/libxrender1/libxcb1: opencv-python
# (pulled in transitively by docling_ibm_models' table-structure model)
# needs these X11/graphics shared libraries at import time even headless --
# same requirement documented in services/offline_data_ingestion/Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY services/data_extraction/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8001

CMD ["uvicorn", "services.data_extraction.main:app", "--host", "0.0.0.0", "--port", "8001"]
