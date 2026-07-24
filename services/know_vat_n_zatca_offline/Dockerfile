# Know VAT & ZATCA -- offline ingestion pipeline.
#
# Separate from services/chatbot/Dockerfile per Mohammed's instruction
# (2026-07-20). This is NOT an always-on service like chatbot/postgres/
# qdrant in docker-compose.yml -- ingestion is a manual/monthly batch job
# (see docs/rag_pipeline_architecture.pdf §1: "Trigger: Manual (monthly, or
# when ZATCA amends a document)"), so it isn't added there. Build and run
# it directly:
#
#   docker build -f services/know_vat_n_zatca_offline/Dockerfile -t know-vat-zatca-offline .
#   docker run --rm --gpus all --network mizan-ai_default \
#       -e QDRANT_URL=http://mizan-qdrant:6333 \
#       -v know_vat_zatca_sqlite:/data \
#       -v <path-to-raw-docs-on-host>:/app/raw \
#       -v <path-to-bge-m3-checkpoint>:/models/bge-m3 \
#       know-vat-zatca-offline \
#       python -m agents.know_vat_n_zatca.offline.ingest \
#           --manifest agents/know_vat_n_zatca/offline/sample_manifest.json \
#           --embedding-model-path /models/bge-m3
#
# --gpus all requires the NVIDIA Container Toolkit installed on the host
# (Docker Desktop's WSL2 backend supports this on Windows) -- without it,
# the container falls back to CPU, which still works but is much slower.
# --network mizan-ai_default connects it to the same Docker network as the
# qdrant service already running via docker-compose.yml.

FROM nvidia/cuda:12.6.0-runtime-ubuntu22.04

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    KNOW_VAT_ZATCA_SQLITE_PATH=/data/know_vat_n_zatca.db

# libgl1/libglib2.0-0/libsm6/libxext6/libxrender1/libxcb1: opencv-python
# (pulled in transitively by docling_ibm_models' table-structure model) needs
# these X11/graphics shared libraries at import time even when only used
# headlessly -- a plain python3/python3-pip base is missing all of them,
# which surfaces as "ImportError: libxcb.so.1: cannot open shared object
# file" (or libGL.so.1, etc.) the first time a PDF is actually converted.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# torch/torchvision installed separately with the CUDA wheel index -- see
# offline/requirements.txt for why: plain `pip install torch` silently
# grabs a CPU-only build, torch must be >=2.6 (transformers refuses to load
# BGE-M3's non-safetensors checkpoint below that, per CVE-2025-32434), and
# torchvision must match whatever torch version this resolves to.
RUN pip install --upgrade pip && \
    pip install "torch>=2.6" --index-url https://download.pytorch.org/whl/cu126 && \
    pip install --upgrade torchvision --index-url https://download.pytorch.org/whl/cu126

COPY agents/know_vat_n_zatca/offline/requirements.txt ./offline-requirements.txt
RUN grep -vE '^(torch|torchvision)(>=.*)?$' offline-requirements.txt > offline-requirements-filtered.txt && \
    pip install -r offline-requirements-filtered.txt

COPY . .

VOLUME ["/data"]
