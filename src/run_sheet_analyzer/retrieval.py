"""Self-contained retrieval layer over a refs/ directory.

Reads the on-disk format produced by the Embeddings DB Creator project;
no external doc_embed package required.

Storage format expected under refs/<name>/:
    manifest.json   – embed_model, citation_template, n_chunks, ...
    chunks.jsonl    – one JSON object per line (id, doc, section, title,
                      parent_hierarchy, citation, text, token_count)
    embeddings.npy  – float32 (n_chunks, embed_dim), pre-built vectors
    bm25.pkl        – pickled BM25Okapi index over the same chunks
"""
from __future__ import annotations

import json
import pickle
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RERANK_MODEL = "rerank-2"
RRF_K = 60
CANDIDATE_POOL = 40


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    doc: str
    section: str
    title: str
    citation: str
    text: str
    score: float
    parent_hierarchy: list


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


class _DocDB:
    """One loaded reference-doc database."""

    def __init__(self, path: Path):
        with open(path / "manifest.json", encoding="utf-8") as f:
            self.manifest: dict = json.load(f)
        self.name: str = self.manifest["name"]
        self.embed_model: str = self.manifest["embed_model"]

        self.chunks: list[dict] = []
        with open(path / "chunks.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.chunks.append(json.loads(line))

        raw = np.load(str(path / "embeddings.npy")).astype(np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        self.embeddings: np.ndarray = raw / np.maximum(norms, 1e-10)

        with open(path / "bm25.pkl", "rb") as f:
            self.bm25 = pickle.load(f)

    def dense_scores(self, query_vec: np.ndarray) -> np.ndarray:
        """Cosine similarity — query_vec must already be L2-normalised."""
        return self.embeddings @ query_vec

    def bm25_scores(self, query: str) -> np.ndarray:
        return np.array(self.bm25.get_scores(_tokenize(query)), dtype=np.float32)


def _rrf(rankings: list[list[int]]) -> list[tuple[int, float]]:
    """Reciprocal rank fusion over multiple ranked lists of chunk indices."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


class RefLibrary:
    def __init__(self, dbs: list[_DocDB]):
        self._dbs = dbs
        self._voyage = None  # created lazily

    def _get_voyage(self):
        if self._voyage is None:
            import voyageai  # imported lazily so startup is fast when refs/ is empty
            self._voyage = voyageai.Client()
        return self._voyage

    def _embed_query(self, query: str, model: str) -> np.ndarray:
        result = self._get_voyage().embed([query], model=model, input_type="query")
        vec = np.array(result.embeddings[0], dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / max(norm, 1e-10)

    @classmethod
    def load(cls, root: str | Path) -> "RefLibrary":
        root = Path(root)
        dbs = []
        if root.exists():
            for subdir in sorted(root.iterdir()):
                if (subdir / "manifest.json").exists():
                    dbs.append(_DocDB(subdir))
        return cls(dbs)

    def stats(self) -> dict:
        return {
            db.name: {
                "n_chunks": db.manifest.get("n_chunks"),
                "embed_model": db.embed_model,
                "built_at": db.manifest.get("built_at"),
                "title": db.manifest.get("title"),
            }
            for db in self._dbs
        }

    def retrieve(
        self,
        query: str,
        *,
        k: int = 8,
        rerank: bool = True,
        docs: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        target_dbs = [db for db in self._dbs if docs is None or db.name in docs]
        if not target_dbs:
            return []

        # Embed query once per distinct model used across target DBs.
        models_needed = {db.embed_model for db in target_dbs}
        query_vecs: dict[str, np.ndarray] = {
            m: self._embed_query(query, m) for m in models_needed
        }

        # Collect top-CANDIDATE_POOL candidates per DB via RRF of dense + BM25.
        all_candidates: list[tuple[_DocDB, int, float]] = []  # (db, chunk_idx, rrf_score)
        for db in target_dbs:
            qvec = query_vecs[db.embed_model]
            dense = db.dense_scores(qvec)
            bm25 = db.bm25_scores(query)

            pool = min(CANDIDATE_POOL, len(db.chunks))
            dense_top = np.argsort(dense)[::-1][:pool].tolist()
            bm25_top = np.argsort(bm25)[::-1][:pool].tolist()

            fused = _rrf([dense_top, bm25_top])[:pool]
            for idx, score in fused:
                all_candidates.append((db, idx, score))

        # Global RRF across all DBs.
        # Build a flat ordering by score then take top candidates for reranking.
        all_candidates.sort(key=lambda x: -x[2])
        top_n = all_candidates[: max(k, CANDIDATE_POOL)]

        chunks = [
            RetrievedChunk(
                id=db.chunks[idx].get("id", ""),
                doc=db.name,
                section=db.chunks[idx].get("section", ""),
                title=db.chunks[idx].get("title", ""),
                citation=db.chunks[idx].get("citation", ""),
                text=db.chunks[idx].get("text", ""),
                score=score,
                parent_hierarchy=db.chunks[idx].get("parent_hierarchy") or [],
            )
            for db, idx, score in top_n
        ]

        if rerank and len(chunks) > 1:
            try:
                texts = [c.text for c in chunks]
                result = self._get_voyage().rerank(
                    query, texts, model=RERANK_MODEL, top_k=k
                )
                reranked = [
                    RetrievedChunk(
                        id=chunks[r.index].id,
                        doc=chunks[r.index].doc,
                        section=chunks[r.index].section,
                        title=chunks[r.index].title,
                        citation=chunks[r.index].citation,
                        text=chunks[r.index].text,
                        score=float(r.relevance_score),
                        parent_hierarchy=chunks[r.index].parent_hierarchy,
                    )
                    for r in result.results
                ]
                return reranked
            except Exception:
                # Rerank is best-effort; fall through to un-reranked top-k.
                pass

        return chunks[:k]
