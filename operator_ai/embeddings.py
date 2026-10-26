"""Local embeddings only.

* "fastembed": BAAI/bge-small-en-v1.5 (384-d) via fastembed, used when the model files are
  already in the local cache (never downloaded at request time).
* "hash": deterministic hashed word + character-trigram embedding (384-d). Always available,
  used by tests and as the fallback.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import numpy as np

from . import config, textnorm

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"
log = logging.getLogger("operator.embeddings")


def cache_dir() -> Path:
    return Path(os.environ.get("OPERATOR_EMBED_CACHE", str(config.ROOT / "data" / "models")))


class HashEmbedder:
    name = "hash-384"

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        toks = textnorm.tokens(text)
        feats = list(toks)
        for t in toks:
            p = f"#{t}#"
            feats += [p[i:i + 3] for i in range(len(p) - 2)]
        feats += [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            idx = h % DIM
            sign = 1.0 if (h >> 32) & 1 else -1.0
            w = 1.0 if "_" in f or len(f) > 3 else 0.35
            v[idx] += sign * w
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        return np.vstack([self._vec(t) for t in texts])


class FastEmbedder:
    name = "bge-small-en-v1.5"

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(MODEL, cache_dir=str(cache_dir()), local_files_only=True)

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        arr = np.array(list(self.model.embed(texts)), dtype=np.float32)
        n = np.linalg.norm(arr, axis=1, keepdims=True)
        n[n == 0] = 1
        return arr / n


_embedder = None
_embedder_key = None


def model_cached() -> bool:
    d = cache_dir()
    return d.exists() and any(p.suffix == ".onnx" for p in d.rglob("*.onnx"))


def get():
    global _embedder, _embedder_key
    mode = config.get().embeddings
    key = (mode, str(cache_dir()))
    if _embedder is not None and _embedder_key == key:
        return _embedder
    emb = None
    if mode in ("auto", "fastembed") and model_cached():
        try:
            emb = FastEmbedder()
        except Exception as e:  # noqa: BLE001
            log.warning("fastembed unavailable, using hashed embeddings: %s", type(e).__name__)
    if emb is None:
        emb = HashEmbedder()
    _embedder, _embedder_key = emb, key
    return emb
