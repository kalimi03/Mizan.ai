"""
Mizan.ai — Modal model-serving layer.

Raw model inference endpoints ONLY. No business logic, no ZATCA knowledge,
no quotas, no tiers — those live in the FastAPI gateway + MCP servers on AWS.

Text in, text out. Pydantic models define the exact contract between the
gateway and these endpoints. Model download/load logic lives in
common/model_loader.py and is shared across classes.

Models (v1 scope — voice and vision cut):
    Qwen2.5-14B-Instruct (GPTQ-Int4)        -> /generate        A10 GPU, agent brain
    Qwen2.5-7B-Instruct (GPTQ-Int4)         -> /generate-lite   T4 GPU, free-tier chat/RAG
    MADLAD-400                              -> /translate       EN<->AR
    BGE-M3                                   -> /embed           1024-dim embeddings

Deploy:  modal deploy modal_app.py

First deploy will be slow (each class downloads its model to the Volume on
first cold start — see startup_timeout below). Subsequent cold starts reuse
the Volume and only pay the vLLM/model-load cost, not the download cost.
"""

from enum import Enum
from typing import List, Optional

import modal
from fastapi import HTTPException
from pydantic import BaseModel, Field

from common.model_loader import (
    ensure_model_on_volume,
    load_embedding_model,
    load_seq2seq_model,
    load_vllm_engine,
    run_embedding,
    run_translation,
    run_vllm_chat,
)

app = modal.App("mizan-models")

# ---------------------------------------------------------------------------
# Images — one per model family, so a change to one doesn't rebuild all.
# .add_local_python_source("common") ships the shared loader module into
# each container explicitly, rather than relying on Modal's implicit
# local-source mounting.
# ---------------------------------------------------------------------------

# VLLM_USE_FLASHINFER_SAMPLER=0: FlashInfer's default sampler JIT-compiles a
# CUDA kernel on first use, which needs nvcc — not present in this slim image
# (only runtime CUDA libs are). Without this, engine startup crashes with
# "Could not find nvcc" the first time a request is sampled (seen on T4).
# Falls back to vLLM's native (non-JIT) sampler instead.
vllm_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("vllm", "fastapi[standard]", "pydantic", "huggingface_hub")
    .env({"VLLM_USE_FLASHINFER_SAMPLER": "0"})
    .add_local_python_source("common")
)

qwen_lite_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("vllm", "fastapi[standard]", "pydantic", "huggingface_hub")
    # Forcing the Triton attention backend (to dodge T4's nvcc-less FlashInfer
    # JIT failure) is done via AttentionConfig in QwenLite.load() below, not
    # an env var — VLLM_ATTENTION_BACKEND is a dead/removed env var as of
    # this vLLM version.
    .env({"VLLM_USE_FLASHINFER_SAMPLER": "0"})
    .add_local_python_source("common")
)

translate_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "transformers", "torch", "sentencepiece", "fastapi[standard]", "pydantic", "huggingface_hub"
    )
    .add_local_python_source("common")
)

embed_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("FlagEmbedding", "torch", "fastapi[standard]", "pydantic", "huggingface_hub")
    .add_local_python_source("common")
)

# Volume holding model weights (downloaded checkpoints persist here across
# container restarts — download-once, reuse-forever until you delete them)
model_volume = modal.Volume.from_name("mizan-model-weights", create_if_missing=True)

MODELS_DIR = "/models"

# All four classes below pull HF_TOKEN from the "huggingface-secret" Modal
# Secret (see each @app.cls(secrets=...)) — not because any repo is gated,
# but because anonymous HF Hub requests get rate-limited/throttled, which can
# stall a checkpoint download entirely under load. Create it once with:
#     modal secret create huggingface-secret HF_TOKEN=hf_xxx

# ---------------------------------------------------------------------------
# Model repo IDs — single source of truth for what gets downloaded
# ---------------------------------------------------------------------------

QWEN_BRAIN_REPO = "Qwen/Qwen2.5-14B-Instruct-GPTQ-Int4"
QWEN_BRAIN_PATH = f"{MODELS_DIR}/qwen2.5-14b-instruct-gptq-int4"

QWEN_LITE_REPO = "Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4"
QWEN_LITE_PATH = f"{MODELS_DIR}/qwen2.5-7b-instruct-gptq-int4"

MADLAD_REPO = "google/madlad400-3b-mt"
MADLAD_PATH = f"{MODELS_DIR}/madlad400-3b-mt"

BGE_M3_REPO = "BAAI/bge-m3"
BGE_M3_PATH = f"{MODELS_DIR}/bge-m3"


# ---------------------------------------------------------------------------
# Shared Pydantic models
# ---------------------------------------------------------------------------


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class Language(str, Enum):
    ar = "ar"
    en = "en"


class ChatMessage(BaseModel):
    role: Role
    content: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# Qwen2.5-14B-Instruct (GPTQ-Int4) — agent brain (A10)
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    messages: List[ChatMessage]
    max_tokens: int = Field(default=1024, gt=0, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    tools: Optional[List[dict]] = None  # tool-calling schemas, passed through to vLLM


class ToolCall(BaseModel):
    name: str
    arguments: dict


class GenerateResponse(BaseModel):
    content: str
    tool_calls: List[ToolCall] = []
    usage: Usage


@app.cls(
    image=vllm_image,
    gpu="A10",
    volumes={MODELS_DIR: model_volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],  # HF_TOKEN — avoids anonymous rate limits
    scaledown_window=300,          # scale to zero after 5 min idle
    startup_timeout=1800,          # generous: first cold start downloads ~10GB checkpoint
    timeout=600,                   # per-request cap — long generations, tool-calling loops
    max_containers=2,              # cost cap for prototype phase; raise once traffic is real
)
@modal.concurrent(max_inputs=4)    # vLLM batches internally; a few concurrent requests per container
class QwenBrain:
    @modal.enter()
    def load(self):
        try:
            path = ensure_model_on_volume(QWEN_BRAIN_PATH, QWEN_BRAIN_REPO, model_volume)
            self.llm = load_vllm_engine(
                path,
                quantization="gptq",
                dtype="float16",
                gpu_memory_utilization=0.9,
            )
        except Exception:
            # Fail loudly at container start rather than surfacing a confusing
            # error on the first request — Modal will mark the container
            # unhealthy and this will show up clearly in `modal deploy` logs.
            import logging
            logging.exception("QwenBrain failed to load — check repo id, quant format, GPU memory.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def generate(self, req: GenerateRequest) -> GenerateResponse:
        try:
            messages = [m.model_dump() for m in req.messages]
            content, tool_calls_raw, usage = run_vllm_chat(
                self.llm,
                messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                tools=req.tools,
            )
            return GenerateResponse(
                content=content,
                tool_calls=[ToolCall(**tc) for tc in tool_calls_raw],
                usage=Usage(**usage),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Generation failed: {exc}")


# ---------------------------------------------------------------------------
# Qwen2.5-7B-Instruct (GPTQ-Int4) — free-tier chat/RAG (T4)
# ---------------------------------------------------------------------------


class GenerateLiteRequest(BaseModel):
    messages: List[ChatMessage]
    max_tokens: int = Field(default=512, gt=0, le=4096)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class GenerateLiteResponse(BaseModel):
    content: str
    usage: Usage


@app.cls(
    image=qwen_lite_image,
    gpu="T4",
    volumes={MODELS_DIR: model_volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],  # HF_TOKEN — avoids anonymous rate limits
    scaledown_window=300,
    startup_timeout=900,           # ~5GB checkpoint — smaller than the brain model
    timeout=300,
    max_containers=2,
)
@modal.concurrent(max_inputs=6)    # small model, more headroom per container
class QwenLite:
    @modal.enter()
    def load(self):
        try:
            # T4 (compute capability 7.5) can't use FlashAttention2 (needs
            # >=8), so vLLM's auto-selection falls back to the FlashInfer
            # backend — which JIT-compiles via nvcc, unavailable in this slim
            # image. Force the Triton backend instead: it compiles its own
            # kernels and doesn't need nvcc. (VLLM_ATTENTION_BACKEND is a
            # dead env var as of this vLLM version — must go through the
            # AttentionConfig passed to LLM() instead.)
            from vllm.config import AttentionConfig

            path = ensure_model_on_volume(QWEN_LITE_PATH, QWEN_LITE_REPO, model_volume)
            self.llm = load_vllm_engine(
                path,
                quantization="gptq",
                dtype="float16",
                gpu_memory_utilization=0.9,
                attention_config=AttentionConfig(backend="TRITON_ATTN"),
            )
        except Exception:
            import logging
            logging.exception("QwenLite failed to load — check repo id, quant format, GPU memory.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def generate_lite(self, req: GenerateLiteRequest) -> GenerateLiteResponse:
        try:
            messages = [m.model_dump() for m in req.messages]
            content, _tool_calls, usage = run_vllm_chat(
                self.llm,
                messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
            )
            return GenerateLiteResponse(content=content, usage=Usage(**usage))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Generation failed: {exc}")


# ---------------------------------------------------------------------------
# MADLAD-400 — EN<->AR translation
# ---------------------------------------------------------------------------


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1)  # plain text chunk; gateway handles doc splitting
    target_language: Language = Language.ar


class TranslateResponse(BaseModel):
    translated_text: str
    target_language: Language


@app.cls(
    image=translate_image,
    gpu="T4",
    volumes={MODELS_DIR: model_volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],  # HF_TOKEN — avoids anonymous rate limits
    scaledown_window=300,
    startup_timeout=900,
    timeout=180,
    max_containers=2,
)
@modal.concurrent(max_inputs=8)
class Translator:
    @modal.enter()
    def load(self):
        try:
            path = ensure_model_on_volume(MADLAD_PATH, MADLAD_REPO, model_volume)
            self.model, self.tokenizer = load_seq2seq_model(path)
        except Exception:
            import logging
            logging.exception("Translator failed to load.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def translate(self, req: TranslateRequest) -> TranslateResponse:
        try:
            translated = run_translation(
                self.model, self.tokenizer, req.text, req.target_language.value
            )
            return TranslateResponse(translated_text=translated, target_language=req.target_language)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Translation failed: {exc}")


# ---------------------------------------------------------------------------
# BGE-M3 — embeddings (RAG indexing + query, agentic memory)
# ---------------------------------------------------------------------------


class EmbedRequest(BaseModel):
    texts: List[str] = Field(..., min_length=1)  # batch of strings


class EmbedResponse(BaseModel):
    embeddings: List[List[float]]
    dim: int = 1024


@app.cls(
    image=embed_image,
    gpu="T4",
    volumes={MODELS_DIR: model_volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],  # HF_TOKEN — avoids anonymous rate limits
    scaledown_window=120,  # embeds are bursty and cheap — shorter idle window
    startup_timeout=600,
    timeout=120,
    max_containers=2,
)
@modal.concurrent(max_inputs=16)   # cheap, fast, high concurrency is safe
class Embedder:
    @modal.enter()
    def load(self):
        try:
            path = ensure_model_on_volume(BGE_M3_PATH, BGE_M3_REPO, model_volume)
            self.model = load_embedding_model(path)
        except Exception:
            import logging
            logging.exception("Embedder failed to load.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def embed(self, req: EmbedRequest) -> EmbedResponse:
        try:
            vectors = run_embedding(self.model, req.texts)
            dim = len(vectors[0]) if vectors else 1024
            return EmbedResponse(embeddings=vectors, dim=dim)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}")