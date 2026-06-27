"""T2S local embedding endpoint — OpenAI-compatible, baked into the container.

Serves a modern SOTA embedding model (Qwen3-Embedding-0.6B) via
sentence-transformers, auto-selecting the device (CUDA -> MPS -> CPU). The T2S
app always talks to this over HTTP at http://localhost:7997/v1, so it needs none
of these libraries itself. Embeddings are NOT user-configurable.
"""
import os
from typing import List, Union

import torch
from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

MODEL_ID = os.environ.get("T2S_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
SERVED_NAME = os.environ.get("T2S_EMBED_SERVED_NAME", "qwen3-embedding")

if torch.cuda.is_available():
    DEVICE = "cuda"
elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
    DEVICE = "mps"
else:
    DEVICE = "cpu"

# Loaded at import time; uvicorn only starts serving once this completes.
_model = SentenceTransformer(MODEL_ID, device=DEVICE)

# Bound the sequence length + input size so a single huge value (e.g. a big
# JSONB column dumped as text) can't make a CPU encode run for minutes and blow
# the client's embedding timeout. 512 tokens / ~4k chars is plenty for semantic
# retrieval over schema descriptions, rules, and knowledge chunks.
MAX_SEQ = int(os.environ.get("T2S_EMBED_MAX_SEQ", "512"))
MAX_CHARS = int(os.environ.get("T2S_EMBED_MAX_CHARS", "4000"))
try:
    _model.max_seq_length = MAX_SEQ
except Exception:  # pragma: no cover
    pass

# Native embedding dimension, auto-detected from the model (no manual config).
try:
    EMBED_DIM = int(_model.get_sentence_embedding_dimension())
except Exception:  # pragma: no cover
    EMBED_DIM = None

app = FastAPI(title="T2S Embeddings")


class EmbeddingRequest(BaseModel):
    input: Union[str, List[str]]
    model: str | None = None
    # Accept (and ignore) other OpenAI fields clients may send.
    encoding_format: str | None = None
    dimensions: int | None = None


@app.get("/health")
def health():
    return {"status": "ok", "model": SERVED_NAME, "device": DEVICE, "dim": EMBED_DIM}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": SERVED_NAME, "object": "model", "owned_by": "t2s"}]}


@app.post("/v1/embeddings")
def create_embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    texts = [(t or "")[:MAX_CHARS] for t in texts]
    vectors = _model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    data = [
        {"object": "embedding", "index": i, "embedding": vec.tolist()}
        for i, vec in enumerate(vectors)
    ]
    tokens = sum(len(t.split()) for t in texts)
    return {
        "object": "list",
        "data": data,
        "model": SERVED_NAME,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
