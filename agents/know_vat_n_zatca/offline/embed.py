"""
Mizan.ai — "Know VAT & ZATCA" local BGE-M3 embedding.

CRITICAL: must produce identical vectors to the Modal-deployed Embedder for
the same input, or retrieval quality degrades silently (no error, just
worse matches) — see docs/rag_pipeline_architecture.pdf §1. The functions
below are a deliberate COPY of modal-serving/common/model_loader.py's
load_embedding_model()/run_embedding(), not a dynamic import — Python
cannot import a package named "modal-serving" (the hyphen makes it an
invalid module name), so the handoff doc's "import or copy" allowance
applies here. If model_loader.py's embedding functions ever change, this
copy must be updated to match — run test_embedding_consistency() (below)
after any change on either side.
"""

from typing import List


def load_embedding_model(model_path: str, use_fp16: bool = True):
    """Load BGE-M3 via FlagEmbedding — copied verbatim from
    modal-serving/common/model_loader.py."""
    from FlagEmbedding import BGEM3FlagModel

    return BGEM3FlagModel(model_path, use_fp16=use_fp16)


def run_embedding(model, texts: List[str]) -> List[List[float]]:
    """Dense vectors only — copied verbatim from
    modal-serving/common/model_loader.py."""
    result = model.encode(texts, return_dense=True, return_sparse=False, return_colbert_vecs=False)
    return result["dense_vecs"].tolist()


def test_embedding_consistency(model, fixed_text: str = "ZATCA VAT compliance test string") -> None:
    """Smoke test — per docs/rag_agent_handoff.pdf offline task 9: embed a
    fixed test string and confirm the output vector shape is what the Modal
    endpoint would produce for the same input, before trusting a full
    ingestion run. This checks shape/determinism locally; a true
    cross-system consistency check requires also calling the live Modal
    /embed endpoint with the same fixed_text and comparing — left as a
    manual step since it needs network access to the deployed endpoint.
    """
    from .. import config

    vectors = run_embedding(model, [fixed_text])
    assert len(vectors) == 1, "expected exactly one vector for one input text"
    assert len(vectors[0]) == config.EMBEDDING_DIM, (
        f"expected {config.EMBEDDING_DIM}-dim vector, got {len(vectors[0])} — "
        "check use_fp16 and model checkpoint match the Modal deployment"
    )
