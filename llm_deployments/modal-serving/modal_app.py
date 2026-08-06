"""
Mizan.ai — Modal model-serving layer.

Raw model inference endpoints ONLY. No business logic, no ZATCA knowledge,
no quotas, no tiers — those live in the FastAPI gateway + MCP servers on AWS.

Text in, text out. Pydantic models define the exact contract between the
gateway and these endpoints. Model download/load logic lives in
common/model_loader.py and is shared across classes.

Models (v1 scope — voice and vision cut):
    Qwen2.5-14B-Instruct (GPTQ-Int4)        -> /generate        A10 GPU, agent brain
    Qwen2.5-7B-Instruct (GPTQ-Int4)         -> /generate-lite   L4 GPU, free-tier chat/RAG
    MADLAD-400                              -> /translate       EN<->AR
    BGE-M3                                   -> /embed           1024-dim embeddings

Deploy:  modal deploy modal_app.py

First deploy will be slow (each class downloads its model to the Volume on
first cold start — see startup_timeout below). Subsequent cold starts reuse
the Volume and only pay the vLLM/model-load cost, not the download cost.
"""

import logging
import time
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = modal.App("mizan-models")


# ---------------------------------------------------------------------------
# Per-request timing — logged only (Modal's own container logs), never
# returned in a response body, so this can never end up visible in the
# frontend or a browser's network tab. This is the practical timing
# available given the offline batch vLLM API used here (LLM.chat() blocks
# until the whole response is generated) — true single-request
# time-to-first-token isn't observable without switching to vLLM's
# streaming/async engine, which is a bigger architectural change. What
# this does capture, for every call: total generation wall-clock time and
# derived output tokens/sec, which is exactly the number that exposed the
# QwenLite T4 slowdown (0.35 tok/s) in the first place.
# ---------------------------------------------------------------------------


def log_generation_timing(model_name: str, elapsed_seconds: float, completion_tokens: Optional[int] = None) -> None:
    if completion_tokens:
        toks_per_sec = completion_tokens / elapsed_seconds if elapsed_seconds > 0 else float("inf")
        logger.info(
            "[timing] %s: %.2fs total, %d completion tokens, %.2f tokens/sec",
            model_name, elapsed_seconds, completion_tokens, toks_per_sec,
        )
    else:
        logger.info("[timing] %s: %.2fs total", model_name, elapsed_seconds)

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
# vllm pinned (not left floating) starting with the GPU-memory-snapshot work
# (docs/mizan_gpu_snapshot_handoff.pdf) -- sleep()/wake_up() and the
# @modal.enter(snap=...) split are APIs that have moved across vLLM
# versions before (see the VLLM_ATTENTION_BACKEND removal note below, which
# already bit this project once) and Modal's own reference examples were
# verified against 0.22.1 specifically. An unpinned "vllm" here would let a
# future unrelated rebuild silently drift onto a version where any of this
# behaves differently or breaks outright.
vllm_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("vllm==0.22.1", "fastapi[standard]", "pydantic", "huggingface_hub")
    .env({"VLLM_USE_FLASHINFER_SAMPLER": "0"})
    .add_local_python_source("common")
)

qwen_lite_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("vllm==0.22.1", "fastapi[standard]", "pydantic", "huggingface_hub")
    # VLLM_USE_FLASHINFER_SAMPLER=0 below: FlashInfer's default sampler
    # JIT-compiles a CUDA kernel on first use, which needs nvcc — not
    # present in this slim image (only runtime CUDA libs are), regardless
    # of GPU tier. Falls back to vLLM's native (non-JIT) sampler instead.
    # (The Triton-attention-backend forcing that used to live here was a
    # separate, T4-specific workaround — removed 2026-07-29 when QwenLite
    # moved to L4, which doesn't need it. See the note on the QwenLite
    # class below.)
    #
    # Cold-start reduction, decided 2026-07-25: two approaches were tried and
    # measured here — enforce_eager=True (skip torch.compile + CUDA graph
    # capture, see QwenLite.load() below) vs VLLM_CACHE_ROOT pointed at the
    # persistent Volume (cache the compile output instead of skipping it).
    # Re-measured end-to-end, accounting for Modal's own container-
    # provisioning overhead (not just vLLM's internal engine-init time):
    # enforce_eager ~68-73s vs cache-persist ~90.55s (baseline 141.2s). The
    # two don't combine — eager mode never compiles, so there's nothing for
    # a compile cache to hold. Going with enforce_eager=True; it's also the
    # simpler option (no Volume cache-management to reason about).
    .env({"VLLM_USE_FLASHINFER_SAMPLER": "0"})
    .add_local_python_source("common")
)

translate_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "transformers>=4.44,<5",  # MADLAD-400 validated on 4.x; v5 breaks <2xx> language-token tokenization
        "torch",
        "sentencepiece",
        "fastapi[standard]",
        "pydantic",
        "huggingface_hub",
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
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    tools: Optional[List[dict]] = None  # tool-calling schemas, passed through to vLLM


class ToolCall(BaseModel):
    name: str
    arguments: dict


class GenerateResponse(BaseModel):
    content: str
    tool_calls: List[ToolCall] = []
    usage: Usage


# GPU memory snapshotting was tried here (docs/mizan_gpu_snapshot_handoff.pdf)
# and reverted 2026-07-25 — Modal's own snapshot-creation step failed after
# our code had already done everything correctly (engine load, torch.compile,
# CUDA graph capture, warmup, sleep(level=1) all succeeded; Modal's own
# infrastructure-level "Failed creating Function memory snapshot" is what
# broke), causing containers to repeatedly fail to start. This is an
# explicitly experimental Modal feature with documented known limitations;
# per the handoff doc's own guidance, application correctness must not
# depend on it succeeding, so it's fully reverted here rather than left
# half-enabled. vllm stays pinned regardless — see the image definitions
# above for why. Follow-up investigation into why Modal's snapshot step
# specifically fails for this config is a separate, non-urgent task.
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
            # enforce_eager=True, decided 2026-07-25 (same trade as
            # QwenLite — see qwen_lite_image above): skips torch.compile +
            # CUDA graph capture. Measured here: cold start 115.79s -> 64.47s
            # end-to-end (engine init 72.53s -> 14.82s).
            self.llm = load_vllm_engine(
                path,
                quantization="gptq",
                dtype="float16",
                gpu_memory_utilization=0.9,
                enforce_eager=True,
            )
        except Exception:
            # Fail loudly at container start rather than surfacing a confusing
            # error on the first request — Modal will mark the container
            # unhealthy and this will show up clearly in `modal deploy` logs.
            logger.exception("QwenBrain failed to load — check repo id, quant format, GPU memory.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def generate(self, req: GenerateRequest) -> GenerateResponse:
        start = time.perf_counter()
        try:
            messages = [m.model_dump() for m in req.messages]
            content, tool_calls_raw, usage = run_vllm_chat(
                self.llm,
                messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                tools=req.tools,
            )
            log_generation_timing("QwenBrain", time.perf_counter() - start, usage.get("completion_tokens"))
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
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class GenerateLiteResponse(BaseModel):
    content: str
    usage: Usage


# GPU memory snapshotting was tried here (docs/mizan_gpu_snapshot_handoff.pdf)
# and reverted 2026-07-25 — see the matching note on QwenBrain above for the
# full reasoning. Same failure mode hit here: Modal's own snapshot-creation
# step failed after our code (engine load, torch.compile, CUDA graph
# capture, warmup, sleep(level=1)) had already succeeded, causing containers
# to repeatedly fail to start.
#
# Moved T4 -> L4 on 2026-07-29: real-world generation on T4 was measured at
# ~0.35 tokens/sec — traced to T4's compute capability (7.5) being below
# vLLM's V1-engine minimum (8.0), forcing a silent fallback to the
# deprecated, much slower V0 engine. L4 (compute capability 8.9) clears
# that bar. The Triton-attention-backend forcing below was a T4-specific
# workaround (T4 can't run FlashAttention2, which needs >=8) — removed
# here since L4 supports it natively; forcing Triton on L4 would just be
# an unnecessary handicap on the exact thing this change is meant to fix.
@app.cls(
    image=qwen_lite_image,
    gpu="L4",
    volumes={MODELS_DIR: model_volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],  # HF_TOKEN — avoids anonymous rate limits
    scaledown_window=300,
    startup_timeout=900,           # ~5GB checkpoint — smaller than the brain model
    timeout=300,
    max_containers=2,
)
@modal.concurrent(max_inputs=5)    # small model, more headroom per container
class QwenLite:
    @modal.enter()
    def load(self):
        try:
            path = ensure_model_on_volume(QWEN_LITE_PATH, QWEN_LITE_REPO, model_volume)
            # enforce_eager=True skips torch.compile + CUDA graph capture
            # entirely — chosen 2026-07-25 over caching the compile output
            # (see qwen_lite_image above for the comparison). Trades ~10-15%
            # steady-state throughput for a faster cold start. Left
            # unchanged here (this change is GPU tier only) so any latency
            # difference measured can be attributed to the GPU swap, not a
            # second variable changing at the same time.
            self.llm = load_vllm_engine(
                path,
                quantization="gptq",
                dtype="float16",
                gpu_memory_utilization=0.9,
                enforce_eager=True,
            )
        except Exception:
            logger.exception("QwenLite failed to load — check repo id, quant format, GPU memory.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def generate_lite(self, req: GenerateLiteRequest) -> GenerateLiteResponse:
        start = time.perf_counter()
        try:
            messages = [m.model_dump() for m in req.messages]
            content, _tool_calls, usage = run_vllm_chat(
                self.llm,
                messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
            )
            log_generation_timing("QwenLite", time.perf_counter() - start, usage.get("completion_tokens"))
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
@modal.concurrent(max_inputs=5)
class Translator:
    @modal.enter()
    def load(self):
        try:
            path = ensure_model_on_volume(MADLAD_PATH, MADLAD_REPO, model_volume)
            self.model, self.tokenizer = load_seq2seq_model(path)
        except Exception:
            logger.exception("Translator failed to load.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def translate(self, req: TranslateRequest) -> TranslateResponse:
        start = time.perf_counter()
        try:
            translated = run_translation(
                self.model, self.tokenizer, req.text, req.target_language.value
            )
            log_generation_timing("Translator", time.perf_counter() - start)
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
            logger.exception("Embedder failed to load.")
            raise

    @modal.fastapi_endpoint(method="POST")
    def embed(self, req: EmbedRequest) -> EmbedResponse:
        start = time.perf_counter()
        try:
            vectors = run_embedding(self.model, req.texts)
            dim = len(vectors[0]) if vectors else 1024
            elapsed = time.perf_counter() - start
            logger.info("[timing] Embedder: %.2fs total, %d text(s)", elapsed, len(req.texts))
            return EmbedResponse(embeddings=vectors, dim=dim)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}")