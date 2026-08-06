"""
Mizan.ai — shared model loading utilities for the Modal serving layer.

Handles:
    - Checking whether a model already exists on the Modal Volume
    - Downloading from Hugging Face Hub if missing, then persisting to the Volume
    - Constructing the actual inference engine:
        * vLLM for chat/causal models (QwenBrain, QwenLite)
        * transformers seq2seq for translation (MADLAD-400)
        * FlagEmbedding for embeddings (BGE-M3)

Imported by modal_app.py inside each class's @modal.enter() hook.
Nothing in this file talks to FastAPI/Pydantic — it only knows about models.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Qwen's Hermes-style tool-calling output format: one or more
# <tool_call>{"name": ..., "arguments": {...}}</tool_call> blocks embedded
# in the generated text. Confirmed against the actual GPTQ checkpoint output.
TOOL_CALL_PATTERN = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _parse_tool_calls(content: str) -> List[dict]:
    """Extract structured tool calls from a Qwen-style generated response."""
    tool_calls = []
    for match in TOOL_CALL_PATTERN.finditer(content):
        try:
            call = json.loads(match.group(1))
            tool_calls.append({"name": call["name"], "arguments": call.get("arguments", {})})
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return tool_calls


# ---------------------------------------------------------------------------
# Download-if-missing (shared by every model type)
# ---------------------------------------------------------------------------


def ensure_model_on_volume(
    local_dir: str,
    hf_repo_id: str,
    volume=None,
    allow_patterns: Optional[List[str]] = None,
) -> str:
    """
    Ensure a model's files exist at `local_dir` (a path inside the mounted
    Modal Volume). If missing, download from Hugging Face Hub and commit
    the Volume so future container starts skip the download.

    Args:
        local_dir: absolute path inside the container, e.g. "/models/qwen3.5-35b"
        hf_repo_id: Hugging Face repo id to pull from if local_dir is empty/missing
        volume: the modal.Volume object mounted at this path — used to commit()
                the download so it's visible to future containers. Pass None to
                skip committing (e.g. in local testing without a real Volume).
        allow_patterns: optional list of filename patterns to restrict the
                         download (e.g. ["*.safetensors", "*.json"])

    Returns:
        local_dir, once the model is confirmed present on disk.
    """
    path = Path(local_dir)
    model_present = path.exists() and any(path.iterdir())

    if model_present:
        logger.info("Model already present at %s — skipping download.", local_dir)
        return local_dir

    logger.info("Model not found at %s — downloading %s from Hugging Face …", local_dir, hf_repo_id)
    path.mkdir(parents=True, exist_ok=True)

    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=hf_repo_id,
        local_dir=local_dir,
        allow_patterns=allow_patterns,
        token=os.environ.get("HF_TOKEN"),  # inject via a Modal Secret; fine as None for public repos
    )

    if volume is not None:
        volume.commit()  # persist the download so future containers don't re-download
        logger.info("Committed downloaded model to Volume.")

    return local_dir


# ---------------------------------------------------------------------------
# vLLM — chat/causal models (QwenBrain, QwenLite)
# ---------------------------------------------------------------------------


def load_vllm_engine(model_path: str, **engine_kwargs):
    """
    Construct a vLLM LLM engine for a causal chat model.

    engine_kwargs are passed straight through to vLLM's LLM(), e.g.
        quantization="gptq", dtype="float16", max_model_len=8192,
        gpu_memory_utilization=0.9
    """
    from vllm import LLM

    logger.info("Loading vLLM engine from %s with kwargs=%s", model_path, engine_kwargs)
    return LLM(model=model_path, **engine_kwargs)


def run_vllm_chat(
    llm,
    messages: List[dict],
    max_tokens: int,
    temperature: float,
    tools: Optional[List[dict]] = None,
) -> Tuple[str, List[dict], dict]:
    """
    Run a chat-style generation through a loaded vLLM engine.

    Args:
        messages: list of {"role": ..., "content": ...} dicts
        tools: optional tool-calling schemas (passed through if the model
               supports vLLM's tool-calling chat template; ignored otherwise)

    Returns:
        (content, tool_calls, usage) where:
            content: the generated text (with any <tool_call> blocks
                     stripped out once they've been parsed into tool_calls)
            tool_calls: list of {"name": ..., "arguments": {...}} dicts
            usage: {"prompt_tokens": int, "completion_tokens": int}
    """
    from vllm import SamplingParams

    sampling_params = SamplingParams(max_tokens=max_tokens, temperature=temperature)

    chat_kwargs = {}
    if tools:
        chat_kwargs["tools"] = tools

    outputs = llm.chat(messages, sampling_params=sampling_params, **chat_kwargs)

    generated = outputs[0].outputs[0]
    content = generated.text
    prompt_tokens = len(outputs[0].prompt_token_ids)
    completion_tokens = len(generated.token_ids)

    tool_calls: List[dict] = []
    if tools:
        tool_calls = _parse_tool_calls(content)
        if tool_calls:
            content = TOOL_CALL_PATTERN.sub("", content).strip()

    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    return content, tool_calls, usage


# ---------------------------------------------------------------------------
# transformers seq2seq — translation (MADLAD-400)
# ---------------------------------------------------------------------------


def load_seq2seq_model(model_path: str, device: str = "cuda"):
    """Load MADLAD-400 via transformers (T5 classes, per Google's official usage)."""
    from transformers import T5ForConditionalGeneration, T5Tokenizer

    logger.info("Loading MADLAD-400 (T5) from %s", model_path)
    tokenizer = T5Tokenizer.from_pretrained(model_path)
    model = T5ForConditionalGeneration.from_pretrained(model_path).to(device)
    model.eval()

    probe_ids = tokenizer("<2ar>", add_special_tokens=False).input_ids
    if len(probe_ids) != 1:
        logger.warning(
            "MADLAD language token '<2ar>' tokenized to %d ids (%s) instead of 1 — "
            "translation quality will likely be broken.",
            len(probe_ids), probe_ids,
        )
    else:
        logger.info("Language token sanity check passed: '<2ar>' -> single id %s", probe_ids)

    return model, tokenizer


def run_translation(model, tokenizer, text: str, target_language: str, device: str = "cuda") -> str:
    """
    Run a single translation through a loaded MADLAD-400 model.

    MADLAD-400 expects a target-language token prefix, e.g. "<2ar>" for Arabic
    or "<2en>" for English, prepended to the source text.
    """
    import torch

    prompt = f"<2{target_language}> {text}"
    inputs = tokenizer(prompt, return_tensors="pt").to(device)

    with torch.no_grad():
        output_ids = model.generate(**inputs, max_new_tokens=1024)

    return tokenizer.decode(output_ids[0], skip_special_tokens=True)


# ---------------------------------------------------------------------------
# FlagEmbedding — embeddings (BGE-M3)
# ---------------------------------------------------------------------------


def load_embedding_model(model_path: str, use_fp16: bool = True):
    """Load BGE-M3 via FlagEmbedding."""
    from FlagEmbedding import BGEM3FlagModel

    logger.info("Loading embedding model from %s", model_path)
    return BGEM3FlagModel(model_path, use_fp16=use_fp16)


def run_embedding(model, texts: List[str]) -> List[List[float]]:
    """
    Run batch embedding through a loaded BGE-M3 model.
    Returns dense vectors only — BGE-M3 also supports sparse/ColBERT vectors,
    which aren't used by Mizan.ai's RAG setup in v1.
    """
    result = model.encode(texts, return_dense=True, return_sparse=False, return_colbert_vecs=False)
    return result["dense_vecs"].tolist()
