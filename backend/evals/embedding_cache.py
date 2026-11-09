"""On-disk cache of embeddings, so repeat eval runs on the same corpus cost nothing."""

import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def cache_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode("utf-8")).hexdigest()


class EmbeddingCache:
    """One JSON line per embedding: {"key", "vector"}. Keyed by model and exact text."""

    def __init__(self, path: Path, model: str, embed_fn: EmbedFn):
        self.path = path
        self.model = model
        self.embed_fn = embed_fn
        self.vectors: dict[str, list[float]] = {}
        if path.exists():
            with path.open(encoding="utf-8") as file:
                for line in file:
                    if line.strip():
                        record = json.loads(line)
                        self.vectors[record["key"]] = record["vector"]

    def missing(self, texts: list[str]) -> list[str]:
        seen, result = set(), []
        for text in texts:
            key = cache_key(self.model, text)
            if key not in self.vectors and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    async def embed(self, texts: list[str]) -> list[list[float]]:
        missing = self.missing(texts)
        if missing:
            vectors = await self.embed_fn(missing)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                for text, vector in zip(missing, vectors):
                    key = cache_key(self.model, text)
                    self.vectors[key] = vector
                    file.write(json.dumps({"key": key, "vector": vector}) + "\n")
        return [self.vectors[cache_key(self.model, text)] for text in texts]
