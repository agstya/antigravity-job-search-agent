"""Chroma embedded vector store for semantic deduplication."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class VectorStore:
    """Wrapper around ChromaDB for semantic job deduplication.

    Uses embedded Chroma — no server required.
    """

    def __init__(self, persist_directory: str = "./chroma_db") -> None:
        self.persist_directory = persist_directory
        Path(persist_directory).mkdir(parents=True, exist_ok=True)
        self._collection = None
        self._client = None

    def _ensure_initialized(self) -> None:
        """Lazy initialization to avoid import cost when not needed."""
        if self._client is not None:
            return
        try:
            import chromadb

            self._client = chromadb.PersistentClient(path=self.persist_directory)
            self._collection = self._client.get_or_create_collection(
                name="job_descriptions",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "Chroma vector store initialized at %s (%d documents)",
                self.persist_directory,
                self._collection.count(),
            )
        except ImportError:
            logger.warning(
                "chromadb not installed — semantic dedup disabled. "
                "Install with: pip install chromadb"
            )
        except Exception as e:
            logger.warning("Failed to initialize Chroma: %s — semantic dedup disabled", e)

    def add_job(self, job_id: str, text: str, metadata: dict | None = None) -> None:
        """Add a job description embedding to the store."""
        self._ensure_initialized()
        if self._collection is None:
            return

        try:
            self._collection.add(
                ids=[job_id],
                documents=[text],
                metadatas=[metadata or {}],
            )
        except Exception as e:
            # Likely duplicate ID
            logger.debug("Failed to add job %s to vector store: %s", job_id, e)

    def find_similar(
        self, text: str, threshold: float = 0.92, n_results: int = 5
    ) -> list[dict]:
        """Find semantically similar jobs above the threshold.

        Chroma uses cosine distance (0 = identical, 2 = opposite).
        We convert threshold from similarity to distance: distance <= 1 - threshold.
        """
        self._ensure_initialized()
        if self._collection is None or self._collection.count() == 0:
            return []

        try:
            results = self._collection.query(
                query_texts=[text],
                n_results=min(n_results, self._collection.count()),
            )

            similar = []
            if results and results["distances"]:
                for i, distance in enumerate(results["distances"][0]):
                    similarity = 1.0 - distance
                    if similarity >= threshold:
                        similar.append(
                            {
                                "id": results["ids"][0][i],
                                "similarity": similarity,
                                "distance": distance,
                            }
                        )
            return similar
        except Exception as e:
            logger.warning("Vector similarity search failed: %s", e)
            return []

    def is_semantic_duplicate(self, text: str, threshold: float = 0.92) -> bool:
        """Check if a very similar job description already exists."""
        similar = self.find_similar(text, threshold=threshold)
        return len(similar) > 0
