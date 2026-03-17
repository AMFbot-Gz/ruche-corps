"""
memory.py — Mémoire persistante et sémantique de La Ruche
  - Episodes: ChromaDB PersistentClient avec embeddings Ollama nomic-embed-text
  - Sessions: Redis avec TTL 48h
  - Recall: recherche vectorielle RÉELLE (cosine similarity)
  - Auto-résumé: sessions longues compressées en 3-5 phrases via Ollama
  - Fallback: si ChromaDB down → Redis seulement, log de l'erreur

3 couches :
  Redis Working  (48h TTL, contexte actif)
  ChromaDB Episodic (permanent, épisodes de conversation)
  ChromaDB Knowledge (faits, préférences, connaissances permanentes)
"""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import chromadb
import httpx
from chromadb.config import Settings

from config import CFG, RUCHE_DIR

logger = logging.getLogger("ruche.memory")

# ─── Chemins persistants ──────────────────────────────────────────────────────
_CHROMA_PATH = RUCHE_DIR / "memory" / "chroma"
_CHROMA_PATH.mkdir(parents=True, exist_ok=True)

# TTL sessions Redis : 48 heures
_SESSION_TTL = 48 * 3600


class RucheMemory:
    """
    Mémoire à 3 couches pour La Ruche.
    Doit être initialisée avec await mem.initialize() avant usage.
    """

    def __init__(self):
        self.cfg = CFG
        self._chroma: Optional[chromadb.ClientAPI] = None
        self._episodes = None    # Collection conversations épisodiques
        self._knowledge = None   # Collection faits / préférences permanents
        self._http = httpx.AsyncClient(timeout=15.0)
        self._redis = None       # Connexion Redis lazy
        self._chroma_ok = False  # Flag : ChromaDB opérationnel

        # 4 collections pour les 4 couches de mémoire
        self._episodic   = None  # Ce qui s'est passé (événements)
        self._semantic   = None  # Ce que j'ai appris (faits + règles)
        self._procedural = None  # Comment faire (séquences réussies)
        self._active     = None  # Contexte immédiat (working memory, TTL court)

    # ─── Initialisation ───────────────────────────────────────────────────────

    async def initialize(self):
        """Connecter ChromaDB (PersistentClient) et créer les collections."""
        try:
            self._chroma = chromadb.PersistentClient(
                path=str(_CHROMA_PATH),
                settings=Settings(anonymized_telemetry=False),
            )
            self._episodes = self._chroma.get_or_create_collection(
                "episodes",
                metadata={"hnsw:space": "cosine"},
            )
            self._knowledge = self._chroma.get_or_create_collection(
                "knowledge",
                metadata={"hnsw:space": "cosine"},
            )
            self._episodic   = self._chroma.get_or_create_collection("episodic",   metadata={"hnsw:space": "cosine"})
            self._semantic   = self._chroma.get_or_create_collection("semantic",   metadata={"hnsw:space": "cosine"})
            self._procedural = self._chroma.get_or_create_collection("procedural", metadata={"hnsw:space": "cosine"})
            self._active     = self._chroma.get_or_create_collection("active",     metadata={"hnsw:space": "cosine"})
            self._chroma_ok = True
            total = self._episodes.count() + self._knowledge.count()
            logger.info(f"[Memory] ChromaDB prête — {total} souvenirs chargés.")
            print(f"[Memory] ChromaDB prête — {total} souvenirs chargés.")
        except Exception as e:
            self._chroma_ok = False
            logger.error(f"[Memory] ChromaDB indisponible: {e} — mode Redis seul activé.")
            print(f"[Memory] ATTENTION: ChromaDB indisponible: {e}")

    async def _get_redis(self):
        """Connexion Redis lazy (réutilisée)."""
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = await aioredis.from_url(self.cfg.REDIS)
        return self._redis

    # ─── Embeddings via Ollama nomic-embed-text ───────────────────────────────

    async def _embed(self, text: str) -> Optional[list[float]]:
        """
        Convertir un texte en vecteur avec nomic-embed-text (local Ollama).
        Retourne None en cas d'erreur — JAMAIS de vecteur nul (vecteur poison).
        """
        try:
            resp = await self._http.post(
                f"{self.cfg.OLLAMA}/api/embeddings",
                json={"model": self.cfg.M_EMBED, "prompt": text},
            )
            resp.raise_for_status()
            embedding = resp.json().get("embedding")
            if not embedding:
                raise ValueError("Réponse embedding vide")
            return embedding
        except Exception as e:
            logger.error(f"[Memory] Embedding error pour '{text[:60]}...': {e}")
            return None  # Skip — on ne sauvegarde PAS un vecteur nul

    # ─── Sauvegarde d'une interaction ─────────────────────────────────────────

    async def save(self, session_id: str, user_text: str, assistant_text: str,
                   metadata: dict = None) -> bool:
        """
        Sauvegarder une interaction dans ChromaDB (épisodique) + Redis (session).

        Retourne True si la sauvegarde ChromaDB a réussi, False sinon.
        Dans tous les cas, sauvegarde dans Redis (working memory).
        """
        text = f"User: {user_text}\nAssistant: {assistant_text}"
        now  = int(time.time())
        doc_id = hashlib.sha256(f"{session_id}:{now}:{user_text[:20]}".encode()).hexdigest()[:16]

        # 1. Sauvegarde Redis (toujours, même si ChromaDB down)
        await self._save_redis_session(session_id, user_text, assistant_text, now)

        # 2. Sauvegarde ChromaDB (vectorielle) — skip si embedding échoue
        if not self._chroma_ok:
            return False

        embedding = await self._embed(text)
        if embedding is None:
            logger.warning(f"[Memory] Skipping ChromaDB save (embedding failed) pour session {session_id}")
            return False

        try:
            self._episodes.add(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[text],
                metadatas=[{
                    "session_id": session_id,
                    "timestamp":  now,
                    "date":       datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"),
                    **(metadata or {}),
                }],
            )
            return True
        except Exception as e:
            logger.error(f"[Memory] ChromaDB add error: {e}")
            return False

    async def _save_redis_session(self, session_id: str, user_text: str,
                                   assistant_text: str, timestamp: int):
        """Sauvegarder un échange dans l'historique Redis de session (TTL 48h)."""
        try:
            r   = await self._get_redis()
            key = f"ruche:session:{session_id}:history"
            entry = json.dumps({
                "user":      user_text,
                "assistant": assistant_text,
                "ts":        timestamp,
            })
            await r.rpush(key, entry)
            await r.expire(key, _SESSION_TTL)
        except Exception as e:
            logger.error(f"[Memory] Redis session save error: {e}")

    # ─── Recherche vectorielle ─────────────────────────────────────────────────

    async def search(self, query: str, n_results: int = 5) -> list[dict]:
        """
        Recherche sémantique vectorielle dans les épisodes (ChromaDB).
        Retourne [] si ChromaDB est indisponible ou en cas d'erreur.

        Chaque résultat : {"text": str, "date": str, "score": float, "session_id": str}
        """
        if not self._chroma_ok:
            return []

        count = self._episodes.count()
        if count == 0:
            return []

        embedding = await self._embed(query)
        if embedding is None:
            return []

        try:
            results = self._episodes.query(
                query_embeddings=[embedding],
                n_results=min(n_results, count),
                include=["documents", "metadatas", "distances"],
            )
            docs   = results["documents"][0]
            metas  = results["metadatas"][0]
            dists  = results["distances"][0]

            output = []
            for doc, meta, dist in zip(docs, metas, dists):
                # dist en cosine dans ChromaDB = 1 - similarity → plus petit = plus proche
                similarity = 1.0 - dist
                output.append({
                    "text":       doc,
                    "date":       meta.get("date", "?"),
                    "session_id": meta.get("session_id", "?"),
                    "score":      round(similarity, 3),
                })
            return output
        except Exception as e:
            logger.error(f"[Memory] ChromaDB search error: {e}")
            return []

    # ─── Historique de session ─────────────────────────────────────────────────

    async def get_session_history(self, session_id: str, max_entries: int = 50) -> list[dict]:
        """
        Récupérer l'historique d'une session depuis Redis.
        Retourne une liste de dicts {"user", "assistant", "ts"}.
        """
        try:
            r   = await self._get_redis()
            key = f"ruche:session:{session_id}:history"
            raw = await r.lrange(key, -max_entries, -1)
            return [json.loads(entry) for entry in raw]
        except Exception as e:
            logger.error(f"[Memory] get_session_history error: {e}")
            return []

    # ─── Auto-résumé de session ───────────────────────────────────────────────

    async def summarize_if_long(self, session_id: str, threshold: int = 20) -> Optional[str]:
        """
        Si une session dépasse `threshold` échanges, génère un résumé 3-5 phrases
        via Ollama et le stocke dans Redis + ChromaDB.

        Retourne le résumé généré, ou None si pas nécessaire / erreur.
        """
        history = await self.get_session_history(session_id, max_entries=200)
        if len(history) < threshold:
            return None

        # Construire le texte de conversation
        conv_text = "\n".join(
            f"User: {h['user']}\nAssistant: {h['assistant']}"
            for h in history[-threshold:]
        )

        prompt = (
            "Résume cette conversation en 3 à 5 phrases concises. "
            "Mets en avant les décisions prises, les informations clés et les préférences exprimées. "
            "Réponds uniquement avec le résumé, sans introduction.\n\n"
            f"Conversation:\n{conv_text[:4000]}"
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                resp = await c.post(
                    f"{self.cfg.OLLAMA}/api/chat",
                    json={
                        "model":   self.cfg.M_FAST,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream":  False,
                        "options": {"temperature": 0.3, "num_predict": 300},
                    },
                )
            summary = resp.json().get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.error(f"[Memory] Summarize Ollama error: {e}")
            return None

        if not summary:
            return None

        # Stocker le résumé dans Redis
        try:
            r         = await self._get_redis()
            sum_key   = f"ruche:session:{session_id}:summary"
            await r.setex(sum_key, _SESSION_TTL, summary)
        except Exception as e:
            logger.error(f"[Memory] Redis summary save error: {e}")

        # Stocker le résumé dans ChromaDB comme épisode compressé
        await self.save(
            session_id=f"summary:{session_id}",
            user_text="[RÉSUMÉ DE SESSION]",
            assistant_text=summary,
            metadata={"type": "summary", "original_session": session_id},
        )

        logger.info(f"[Memory] Résumé généré pour session {session_id} ({len(history)} échanges)")
        return summary

    # ─── Mémoriser un fait permanent ──────────────────────────────────────────

    async def remember_fact(self, fact: str, category: str = "general") -> bool:
        """
        Sauvegarder un fait ou une préférence dans la mémoire sémantique (knowledge).
        Utilise upsert pour éviter les doublons.
        Retourne True si sauvegardé dans ChromaDB, False sinon.
        """
        # Sauvegarde Redis en backup rapide (30 jours)
        try:
            r   = await self._get_redis()
            key = f"ruche:fact:{hashlib.sha256(fact.encode()).hexdigest()[:12]}"
            await r.setex(key, 86400 * 30, json.dumps({
                "fact":      fact,
                "category":  category,
                "timestamp": int(time.time()),
            }))
        except Exception as e:
            logger.error(f"[Memory] Redis fact save error: {e}")

        if not self._chroma_ok:
            return False

        embedding = await self._embed(fact)
        if embedding is None:
            logger.warning(f"[Memory] Skipping ChromaDB fact save (embedding failed)")
            return False

        try:
            doc_id = hashlib.sha256(fact.encode()).hexdigest()[:16]
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
            logger.info(f"[Memory] Fait mémorisé ({category}): {fact[:80]}")
            return True
        except Exception as e:
            logger.error(f"[Memory] ChromaDB fact upsert error: {e}")
            return False

    # ─── Recherche dans les faits ──────────────────────────────────────────────

    async def search_facts(self, query: str, n: int = 3) -> list[dict]:
        """
        Recherche sémantique dans la mémoire de faits (knowledge).
        Retourne [] si ChromaDB est indisponible.
        """
        if not self._chroma_ok:
            return []

        count = self._knowledge.count()
        if count == 0:
            return []

        embedding = await self._embed(query)
        if embedding is None:
            return []

        try:
            results = self._knowledge.query(
                query_embeddings=[embedding],
                n_results=min(n, count),
                include=["documents", "metadatas", "distances"],
            )
            docs   = results["documents"][0]
            dists  = results["distances"][0]
            metas  = results["metadatas"][0]
            return [
                {
                    "text":     doc,
                    "category": meta.get("category", "general"),
                    "score":    round(1.0 - dist, 3),
                }
                for doc, dist, meta in zip(docs, dists, metas)
            ]
        except Exception as e:
            logger.error(f"[Memory] search_facts error: {e}")
            return []

    # ─── Contexte enrichi pour injection dans le prompt ───────────────────────

    async def get_context_for_query(self, query: str, session_id: str = "") -> str:
        """
        Construire un bloc de contexte à injecter dans le prompt de l'agent :
        - Résumé de session actuelle (Redis)
        - Épisodes sémantiquement proches (ChromaDB)
        - Faits pertinents (ChromaDB knowledge)
        - Règles apprises pertinentes (SynapseLayer — core/learning.py)

        Retourne une chaîne formatée prête à être insérée dans le prompt.
        """
        parts = []

        # 1. Résumé de session actuelle
        if session_id:
            try:
                r       = await self._get_redis()
                sum_key = f"ruche:session:{session_id}:summary"
                summary = await r.get(sum_key)
                if summary:
                    parts.append(f"[Résumé session actuelle]\n{summary.decode()}")
            except Exception:
                pass

        # 2. Épisodes similaires (recherche vectorielle)
        episodes = await self.search(query, n_results=4)
        if episodes:
            ep_lines = []
            for ep in episodes:
                if ep["score"] >= 0.3:  # Filtrer les trop lointains
                    ep_lines.append(f"[{ep['date']} — similarité {ep['score']}]\n{ep['text'][:300]}")
            if ep_lines:
                parts.append("[Souvenirs pertinents]\n" + "\n---\n".join(ep_lines))

        # 3. Faits mémorisés pertinents
        facts = await self.search_facts(query, n=3)
        if facts:
            fact_lines = [
                f"• [{f['category']}] {f['text']}"
                for f in facts if f["score"] >= 0.3
            ]
            if fact_lines:
                parts.append("[Faits mémorisés]\n" + "\n".join(fact_lines))

        # 4. Règles apprises pertinentes (SynapseLayer)
        try:
            from core.learning import get_learning_engine
            rules = get_learning_engine().get_rules_for_query(query, max_rules=4)
            if rules:
                parts.append("[Règles apprises]\n" + "\n".join(f"• {r}" for r in rules))
        except Exception:
            pass

        return "\n\n".join(parts) if parts else ""

    # ─── Recherche compatible ancienne API ────────────────────────────────────

    async def search_relevant(self, query: str, n: int = 4,
                               session_id: str = None) -> str:
        """Alias de search() formaté en texte — compatibilité avec l'agent."""
        episodes = await self.search(query, n_results=n)
        if not episodes:
            return ""
        parts = []
        for ep in episodes:
            if ep["score"] >= 0.15:
                parts.append(f"[{ep['date']}] {ep['text'][:300]}")
        return "\n---\n".join(parts) if parts else ""

    # ─── Oublier une session ──────────────────────────────────────────────────

    async def forget(self, session_id: str) -> dict:
        """
        Effacer toutes les données d'une session :
        - Historique Redis
        - Résumé Redis
        - Épisodes ChromaDB (tous ceux ayant session_id correspondant)

        Retourne un dict {"redis_deleted": int, "chroma_deleted": int}
        """
        redis_deleted  = 0
        chroma_deleted = 0

        # Nettoyage Redis
        try:
            r    = await self._get_redis()
            keys = [
                f"ruche:session:{session_id}:history",
                f"ruche:session:{session_id}:summary",
            ]
            for key in keys:
                deleted = await r.delete(key)
                redis_deleted += deleted
        except Exception as e:
            logger.error(f"[Memory] forget Redis error: {e}")

        # Nettoyage ChromaDB
        if self._chroma_ok:
            try:
                results = self._episodes.get(where={"session_id": session_id})
                ids_to_delete = results.get("ids", [])
                if ids_to_delete:
                    self._episodes.delete(ids=ids_to_delete)
                    chroma_deleted = len(ids_to_delete)
            except Exception as e:
                logger.error(f"[Memory] forget ChromaDB error: {e}")

        logger.info(f"[Memory] forget({session_id}): redis={redis_deleted}, chroma={chroma_deleted}")
        return {"redis_deleted": redis_deleted, "chroma_deleted": chroma_deleted}

    # ─── Enregistrement d'apprentissage après mission ─────────────────────────

    async def record_mission_outcome(
        self,
        goal: str,
        result: str,
        success: bool,
    ) -> bool:
        """
        Après l'exécution d'une mission :
        - Sauvegarde l'épisode dans ChromaDB
        - Si succès : extrait une règle via SynapseLayer (non bloquant)

        Retourne True si au moins la sauvegarde Redis a réussi.
        """
        session_id = f"mission:{hash(goal) & 0xFFFF:04x}"
        saved = await self.save(
            session_id=session_id,
            user_text=f"[MISSION] {goal}",
            assistant_text=result[:500],
            metadata={"type": "mission", "success": str(success)},
        )

        # Extraction de règle en arrière-plan si succès
        if success:
            try:
                from core.learning import get_learning_engine
                engine = get_learning_engine()
                asyncio.create_task(
                    engine._synapse.extract_rule_from_mission(goal, result)
                )
            except Exception:
                pass

        return saved

    # ─── Mémoire procédurale ──────────────────────────────────────────────────

    async def store_procedural(self, task: str, tool_sequence: list[str], result: str,
                                success: bool, confidence: float = 0.8):
        """Mémorise une séquence d'outils qui a (bien ou mal) fonctionné."""
        if not self._chroma_ok or self._procedural is None:
            return

        text = f"{task}\n{result}"
        embedding = await self._embed(text)
        if embedding is None:
            return

        doc_id = hashlib.sha256(f"{task}{str(tool_sequence)}".encode()).hexdigest()[:16]
        try:
            self._procedural.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[text],
                metadatas=[{
                    "success":    str(success),
                    "tools":      json.dumps(tool_sequence),
                    "confidence": confidence,
                    "timestamp":  datetime.now().isoformat(),
                }],
            )
            logger.info(f"[Memory] Procédure mémorisée (success={success}): {task[:60]}")
        except Exception as e:
            logger.error(f"[Memory] store_procedural error: {e}")

    async def get_procedural(self, task: str, n: int = 3) -> list[dict]:
        """Retrouve les séquences réussies similaires à cette tâche."""
        if not self._chroma_ok or self._procedural is None:
            return []

        count = self._procedural.count()
        if count == 0:
            return []

        embedding = await self._embed(task)
        if embedding is None:
            return []

        try:
            results = self._procedural.query(
                query_embeddings=[embedding],
                n_results=min(n * 2, count),
                include=["documents", "metadatas", "distances"],
                where={"success": "True"},
            )
            docs  = results["documents"][0]
            metas = results["metadatas"][0]
            dists = results["distances"][0]

            output = []
            for doc, meta, dist in zip(docs, metas, dists):
                output.append({
                    "text":       doc,
                    "tools":      json.loads(meta.get("tools", "[]")),
                    "confidence": meta.get("confidence", 0.5),
                    "score":      round(1.0 - dist, 3),
                })
            return output[:n]
        except Exception as e:
            logger.error(f"[Memory] get_procedural error: {e}")
            return []

    # ─── Mémoire sémantique ───────────────────────────────────────────────────

    async def store_semantic(self, fact: str, source: str, confidence: float = 0.9):
        """Mémorise un fait ou une règle généralisée avec score de confiance."""
        if not self._chroma_ok or self._semantic is None:
            return

        embedding = await self._embed(fact)
        if embedding is None:
            return

        doc_id = hashlib.sha256(fact.encode()).hexdigest()[:16]
        try:
            self._semantic.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[fact],
                metadatas=[{
                    "source":     source,
                    "confidence": confidence,
                    "timestamp":  datetime.now().isoformat(),
                }],
            )
            logger.info(f"[Memory] Fait sémantique mémorisé (conf={confidence}): {fact[:60]}")
        except Exception as e:
            logger.error(f"[Memory] store_semantic error: {e}")

    async def get_semantic(self, query: str, min_confidence: float = 0.6, n: int = 5) -> list[dict]:
        """Retrouve les faits/règles pertinents avec confiance >= min_confidence."""
        if not self._chroma_ok or self._semantic is None:
            return []

        count = self._semantic.count()
        if count == 0:
            return []

        embedding = await self._embed(query)
        if embedding is None:
            return []

        try:
            results = self._semantic.query(
                query_embeddings=[embedding],
                n_results=min(n * 2, count),
                include=["documents", "metadatas", "distances"],
            )
            docs  = results["documents"][0]
            metas = results["metadatas"][0]
            dists = results["distances"][0]

            output = []
            for doc, meta, dist in zip(docs, metas, dists):
                conf = float(meta.get("confidence", 0.0))
                if conf >= min_confidence:
                    output.append({
                        "text":       doc,
                        "source":     meta.get("source", ""),
                        "confidence": conf,
                        "score":      round(1.0 - dist, 3),
                    })
            return output[:n]
        except Exception as e:
            logger.error(f"[Memory] get_semantic error: {e}")
            return []

    # ─── Working memory (active) ──────────────────────────────────────────────

    async def set_active(self, key: str, value: str, ttl_minutes: int = 60):
        """Working memory : contexte immédiat avec TTL."""
        if not self._chroma_ok or self._active is None:
            return

        # Nettoyer les entrées expirées avant d'insérer
        now_iso = datetime.now().isoformat()
        try:
            all_entries = self._active.get(include=["metadatas"])
            ids_to_delete = [
                entry_id
                for entry_id, meta in zip(
                    all_entries.get("ids", []),
                    all_entries.get("metadatas", [])
                )
                if meta.get("expires_at", "") < now_iso
            ]
            if ids_to_delete:
                self._active.delete(ids=ids_to_delete)
        except Exception as e:
            logger.warning(f"[Memory] set_active cleanup error: {e}")

        embedding = await self._embed(f"{key}: {value}")
        if embedding is None:
            return

        expires_at = datetime.fromtimestamp(
            time.time() + ttl_minutes * 60
        ).isoformat()
        doc_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        try:
            self._active.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[f"{key}: {value}"],
                metadatas=[{
                    "key":        key,
                    "value":      value,
                    "expires_at": expires_at,
                }],
            )
        except Exception as e:
            logger.error(f"[Memory] set_active error: {e}")

    async def get_active(self, key: str) -> Optional[str]:
        """Récupère une valeur de la working memory si pas expirée."""
        if not self._chroma_ok or self._active is None:
            return None

        doc_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        try:
            result = self._active.get(ids=[doc_id], include=["metadatas"])
            if not result["ids"]:
                return None
            meta = result["metadatas"][0]
            expires_at = meta.get("expires_at", "")
            if expires_at < datetime.now().isoformat():
                self._active.delete(ids=[doc_id])
                return None
            return meta.get("value")
        except Exception as e:
            logger.error(f"[Memory] get_active error: {e}")
            return None

    # ─── Contexte complet pour une requête ────────────────────────────────────

    async def get_full_context(self, query: str) -> dict:
        """
        Retourne le contexte complet pour une requête:
        {
          episodic: [...],    # 3 épisodes pertinents
          semantic: [...],    # 5 faits/règles pertinents (confidence >= 0.6)
          procedural: [...],  # 3 séquences réussies similaires
          active: {...},      # working memory courante
        }
        Utilisé par agent.py pour enrichir le contexte de chaque requête.
        """
        # Exécuter les 3 recherches en parallèle
        episodic_task   = asyncio.create_task(self.search(query, n_results=3))
        semantic_task   = asyncio.create_task(self.get_semantic(query, min_confidence=0.6, n=5))
        procedural_task = asyncio.create_task(self.get_procedural(query, n=3))

        episodic_results, semantic_results, procedural_results = await asyncio.gather(
            episodic_task, semantic_task, procedural_task
        )

        # Lire toute la working memory active non expirée
        active_data = {}
        if self._chroma_ok and self._active is not None:
            try:
                now_iso = datetime.now().isoformat()
                all_entries = self._active.get(include=["metadatas"])
                for meta in all_entries.get("metadatas", []):
                    if meta.get("expires_at", "") >= now_iso:
                        active_data[meta["key"]] = meta["value"]
            except Exception as e:
                logger.warning(f"[Memory] get_full_context active error: {e}")

        return {
            "episodic":   episodic_results,
            "semantic":   semantic_results,
            "procedural": procedural_results,
            "active":     active_data,
        }

    # ─── Stats ────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "chroma_ok":  self._chroma_ok,
            "episodes":   self._episodes.count()  if self._chroma_ok and self._episodes  else 0,
            "knowledge":  self._knowledge.count() if self._chroma_ok and self._knowledge else 0,
        }

    # ─── Fermeture propre ─────────────────────────────────────────────────────

    async def close(self):
        """Fermer les connexions proprement."""
        try:
            await self._http.aclose()
        except Exception:
            pass
        try:
            if self._redis is not None:
                await self._redis.aclose()
        except Exception:
            pass


# ─── Instance globale (importée par les outils et l'agent) ────────────────────
# Usage: from memory import RucheMemory; mem = RucheMemory(); await mem.initialize()

# Alias pour compatibilité avec l'import de l'agent
Memory = RucheMemory
