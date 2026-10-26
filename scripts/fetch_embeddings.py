#!/usr/bin/env python3
"""One-time, online: download the local embedding model (BAAI/bge-small-en-v1.5) into the
model cache. After this the app embeds locally with no network access."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import embeddings  # noqa: E402

from fastembed import TextEmbedding  # noqa: E402

d = embeddings.cache_dir()
d.mkdir(parents=True, exist_ok=True)
TextEmbedding(embeddings.MODEL, cache_dir=str(d))
print(f"model cached in {d}; restart the app (OPERATOR_EMBEDDINGS=auto picks it up)")
