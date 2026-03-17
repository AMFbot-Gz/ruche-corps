"""
memory.py — Mémoire vectorielle de La Ruche
ChromaDB + nomic-embed-text (déjà installé dans Ollama)

Amélioration vs PicoClaw :
  PicoClaw = MEMORY.md (fichier plat, pas de recherche sémantique)
  Ruche    = Vector DB → "souviens-toi des conversations pertinentes
             automatiquement" + historique sémantique illimité

3 couches :
  🔴 Working  → Redis  (2h TTL, contexte actif)
  🟡 Episodic → ChromaDB (permanent, recherche sémantique)
  🟢 Semantic → ChromaDB (faits, préférences, connaissances)
"""
import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import chromadb
import httpx
from chromadb.config import Settings

from config import CFG as CONFIG, RUCHE_DIR


# ─── ChromaDB setup ───────────────────────────────────────────────────────────
_CHROMA_PATH = RUCHE_DIR / "memory" / "chroma"
_CHROMA_PATH.mkdir(parents=True, exist_ok=True)


class Memory:
    def __init__(self):
        self.cfg = CONFIG
        self._chroma: Optional[chromadb.ClientAPI] = None
        self._episodes = None       # Collection conversations
        self._knowledge = None      # Collection faits/connaissances
        self._http = httpx.AsyncClient(timeout=10.0)

    # ─── Init ──────────────────────────────────────────────────────────────
    async def initialize(self):
        """Connecter ChromaDB et créer les collections."""
        self._chroma = chromadb.PersistentClient(
            path=str(_CHROMA_PATH),
            settings=Settings(anonymized_telemetry=False),
        )
        self._episodes  = self._chroma.get_or_create_collection(
            "episodes",
            metadata={"hnsw:space": "cosine"},
        )
        self._knowledge = self._chroma.get_or_create_collection(
            "knowledge",
            metadata={"hnsw:space": "cosine"},
        )
        total = self._episodes.count() + self._knowledge.count()
        print(f"[Memory] ChromaDB prête — {total} souvenirs chargés.")

    # ─── Embeddings via Ollama nomic-embed-text ───────────────────────────
    async def _embed(self, text: str) -> list[float]:
        """Convertir un texte en vecteur avec nomic-embed-text (local)."""
        try:
            resp = await self._http.post(
                f"{self.cfg.OLLAMA}/api/embeddings",
                json={"model": self.cfg.M_EMBED, "prompt": text},
            )
            return resp.json()["embedding"]
        except Exception as e:
            # Fallback : vecteur nul (mieux que crash)
            print(f"[Memory] Embedding error: {e}")
            return [0.0] * 768

    # ─── Sauvegarder une interaction ──────────────────────────────────────
    async def save(self, session_id: str, user_msg: str, assistant_msg: str,
                   metadata: dict = None):
        """Sauvegarder une interaction en mémoire épisodique."""
        text = f"User: {user_msg}\nAssistant: {assistant_msg}"
        doc_id = hashlib.sha256(f"{session_id}:{time.time()}".encode()).hexdigest()[:16]
        embedding = await self._embed(text)
        self._episodes.add(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "session_id": session_id,
                "timestamp":  int(time.time()),
                "date":       datetime.now().strftime("%Y-%m-%d %H:%M"),
                **(metadata or {}),
            }],
        )

    # ─── Chercher contexte pertinent ──────────────────────────────────────
    async def search_relevant(self, query: str, n: int = 4,
                               session_id: str = None) -> str:
        """Chercher les souvenirs les plus proches sémantiquement."""
        if self._episodes.count() == 0:
            return ""
        embedding = await self._embed(query)
        where = {"session_id": session_id} if session_id else None
        try:
            results = self._episodes.query(
                query_embeddings=[embedding],
                n_results=min(n, self._episodes.count()),
                where=where,
                include=["documents", "metadatas", "distances"],
            )
            docs = results["documents"][0]
            metas = results["metadatas"][0]
            dists = results["distances"][0]
            if not docs:
                return ""
            parts = []
            for doc, meta, dist in zip(docs, metas, dists):
                if dist > 0.85:          # Trop lointain sémantiquement
                    continue
                date = meta.get("date", "?")
                parts.append(f"[{date}] {doc[:300]}")
            return "\n---\n".join(parts) if parts else ""
        except Exception as e:
            print(f"[Memory] Search error: {e}")
            return ""

    # ─── Mémoriser un fait permanent ──────────────────────────────────────
    async def remember_fact(self, fact: str, category: str = "general"):
        """Sauvegarder un fait ou une préférence dans la mémoire sémantique."""
        doc_id = hashlib.sha256(fact.encode()).hexdigest()[:16]
        embedding = await self._embed(fact)
        self._knowledge.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[fact],
            metadatas=[{
                "category":  category,
                "timestamp": int(time.time()),
                "date":      datetime.now().strftime("%Y-%m-%d"),
            }],
        )
        print(f"[Memory] Fait mémorisé ({category}): {fact[:80]}")

    # ─── Chercher dans les faits ──────────────────────────────────────────
    async def search_facts(self, query: str, n: int = 3) -> str:
        if self._knowledge.count() == 0:
            return ""
        embedding = await self._embed(query)
        try:
            results = self._knowledge.query(
                query_embeddings=[embedding],
                n_results=min(n, self._knowledge.count()),
                include=["documents", "distances"],
            )
            docs  = results["documents"][0]
            dists = results["distances"][0]
            return "\n".join(d for d, dist in zip(docs, dists) if dist < 0.8)
        except Exception:
            return ""

    # ─── Stats ────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        return {
            "episodes":  self._episodes.count()  if self._episodes  else 0,
            "knowledge": self._knowledge.count() if self._knowledge else 0,
        }

    async def close(self):
        await self._http.aclose()
