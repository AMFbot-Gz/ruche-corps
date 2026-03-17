"""
agent.py — Cerveau de La Ruche v2.0

Architecture Claude :
  - Nemotron-3-Super comme modèle principal (230B, contexte 128K)
  - Context Builder injecte les fichiers pertinents dans le prompt
  - Boucle ReAct : Penser → Appeler outils → Observer → Itérer
  - Exécution parallèle des outils indépendants
  - Mémoire vectorielle + sessions Redis TTL 2h
  - Streaming token-par-token vers ruche:stream
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime

import httpx
import redis.asyncio as aioredis
from pydantic import ValidationError

from config import CFG
from core.logger import get_logger
from core.resilience import get_ollama_client, CircuitOpenError
from core.schemas import InboundMessage
from core.thinking import get_thinking_layer
from memory import Memory
from router import Router
from tools.registry import registry
from context.builder import ContextBuilder

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT — Définit l'identité et les capacités
# ═══════════════════════════════════════════════════════════════
SYSTEM = """\
Tu es {name} — agent IA souverain et spécialiste universel de {owner}.
Tu disposes de {n_tools} outils pour agir directement sur le Mac, le web, le code et la mémoire.

CAPACITÉS COMPLÈTES :
→ Développement : Python, JS, Go, Rust, SQL, bash, tout langage
→ Computer Use  : voir l'écran, cliquer, taper, ouvrir des apps, scripter macOS
→ Fichiers      : lire, écrire, modifier, analyser n'importe quel fichier
→ Web           : rechercher, récupérer, analyser des pages
→ GitHub        : repos, issues, PRs, recherche de code
→ Code          : éditer avec aider+qwen3-coder, analyser, refactoriser
→ Mémoire       : mémoriser et rappeler des faits importants
→ Contexte 1M   : charger des projets entiers avec load_context()

STRATÉGIE DE TRAVAIL (ReAct) :
1. ANALYSER l'objectif réel (pas juste la demande de surface)
2. PLANIFIER les étapes en silence (pas besoin de les énoncer)
3. AGIR avec les outils dans l'ordre le plus efficace
4. VÉRIFIER le résultat de chaque étape
5. CORRIGER automatiquement si ça ne marche pas
6. RÉPONDRE clairement une fois terminé

RÈGLES ABSOLUES :
- Tu agis, tu ne délègues pas et tu ne demandes pas de confirmation
- "Je ne peux pas" n'existe pas — trouver le chemin alternatif
- Pour le code : utiliser code_edit() avec aider (Claude Code local)
- Pour l'écran : utiliser see_screen() avant de cliquer
- Pour les projets complexes : utiliser load_context() pour charger les fichiers
- Pour les questions très difficiles : utiliser mixture_answer()
- Mémoriser avec remember() ce qui sera utile plus tard

OUTILS DISPONIBLES : {tools}

DATE/HEURE : {dt}
{context}
"""


class RucheAgent:
    def __init__(self):
        self.redis          = None
        self.memory         = Memory()
        self.router         = Router()
        self.ctx            = ContextBuilder()
        # _http conservé pour les appels non-Ollama (streaming, etc.)
        self._http          = httpx.AsyncClient(timeout=180.0)
        self._thinking      = get_thinking_layer()
        self._autonomy_level = int(os.getenv("AUTONOMY_LEVEL", "3"))
        import tools.builtins  # enregistrement @tool

    # ── Démarrage ─────────────────────────────────────────────
    async def start(self):
        self.redis = await aioredis.from_url(CFG.REDIS)
        await self.redis.ping()
        await self.memory.initialize()
        tools_list = registry.list_tools()
        log.info("agent_started",
                 redis="ok",
                 chromadb="ok",
                 tool_count=len(tools_list),
                 tools=", ".join(tools_list),
                 model=CFG.M_GENERAL,
                 ctx_k=CFG.NEMOTRON_CTX // 1000,
                 channel=CFG.CH_IN)
        async with self.redis.pubsub() as ps:
            await ps.subscribe(CFG.CH_IN)
            async for msg in ps.listen():
                if msg["type"] == "message":
                    try:
                        asyncio.create_task(self._dispatch(json.loads(msg["data"])))
                    except Exception as e:
                        log.error("dispatch_error", error=str(e))

    # ── Dispatch d'un message ─────────────────────────────────
    async def _dispatch(self, data: dict):
        t0 = time.monotonic()

        # Validation stricte du message entrant
        try:
            msg = InboundMessage.model_validate(data)
        except ValidationError as e:
            log.error("inbound_validation_error",
                      errors=e.errors(),
                      raw_data=str(data)[:200])
            return

        channel = msg.channel
        uid     = msg.user_id
        text    = msg.text.strip()
        sid     = msg.session_id if msg.session_id != "unknown" else f"{channel}:{uid}"

        if not text:
            return

        log.info("message_received", channel=channel, text_preview=text[:80], session_id=sid)

        # Routage modèle
        route = await self.router.classify(text)
        model = route.model
        log.info("route_selected",
                 model=model,
                 reasoning_type=route.reasoning_type,
                 latency_ms=round(route.latency_ms, 1),
                 session_id=sid)

        # Mémoire vectorielle
        mem_ctx   = await self.memory.search_relevant(text, n=3, session_id=sid)
        facts_ctx = await self.memory.search_facts(text, n=2)
        mem_text  = "\n".join(filter(None, [mem_ctx, facts_ctx])) or ""

        # Context builder — auto-détection des fichiers pertinents
        file_ctx = ""
        if any(kw in text.lower() for kw in ["fichier", "code", "projet", "analyser", "lire", "charger"]):
            auto_files = self.ctx.auto_files_for_query(text)
            if auto_files:
                file_ctx = self.ctx.build(query=text, files=auto_files, memory=mem_text)

        context_block = ""
        if file_ctx:
            context_block = f"\n📂 CONTEXTE AUTO-CHARGÉ:\n{file_ctx[:8000]}\n"
        elif mem_text:
            context_block = f"\n📡 MÉMOIRE:\n{mem_text[:1000]}\n"

        # Historique session
        history = await self._history_get(sid)

        # Passe de raisonnement silencieux (ThinkingLayer, M_FAST, non-bloquant)
        thought = await self._thinking.think(text, context_summary=mem_text[:300])

        # Injection de l'analyse interne dans le system prompt
        thinking_injection = thought.to_system_injection()

        # Avertissement si confiance basse (niveau autonomie 3 par défaut)
        if self._thinking.should_ask_confirmation(thought, self._autonomy_level):
            thinking_injection += (
                "\n\nTa confiance est basse. "
                "Si tu as un doute, demande confirmation avant une action irréversible."
            )

        # System prompt avec Nemotron
        system = SYSTEM.format(
            name=CFG.NAME,
            owner=CFG.OWNER,
            n_tools=len(registry.list_tools()),
            tools=", ".join(registry.list_tools()),
            dt=datetime.now().strftime("%A %d %B %Y — %H:%M"),
            context=context_block,
        ) + f"\n\n{thinking_injection}"

        messages = history + [{"role": "user", "content": text}]
        answer, tool_calls_used = await self._loop(model, system, messages, sid)

        # Persistance de l'épisode
        await self.memory.save(sid, text, answer)

        # Mémorisation procédurale de la séquence réussie
        if tool_calls_used:
            await self.memory.store_procedural(
                task=text,
                tool_sequence=[tc["name"] for tc in tool_calls_used],
                result=answer[:200],
                success=True,
                confidence=thought.confidence,
            )
        await self._history_set(sid, text, answer)

        ms = (time.monotonic() - t0) * 1000
        log.info("response_sent",
                 session_id=sid,
                 model=model,
                 ms=round(ms, 1),
                 answer_preview=answer[:60])

        await self.redis.publish(CFG.CH_OUT, json.dumps({
            "channel":    channel,
            "user_id":    uid,
            "session_id": sid,
            "response":   answer,
            "model":      model,
            "ms":         ms,
        }))

    # ── Boucle ReAct LLM ↔ Outils ────────────────────────────
    async def _loop(self, model: str, system: str, messages: list,
                    sid: str, max_iter: int = 15) -> tuple[str, list]:
        tools = registry.get_schemas()
        msgs  = messages.copy()

        # Options Nemotron : grand contexte + température calibrée
        is_nemotron = "nemotron" in model.lower()
        options = {
            "temperature": CFG.NEMOTRON_TEMP if is_nemotron else 0.7,
            "num_predict": 3000,
        }
        if is_nemotron:
            options["num_ctx"] = CFG.NEMOTRON_CTX

        ollama = get_ollama_client()

        # Accumulation de tous les tool_calls utilisés dans la session
        all_tool_calls: list[dict] = []

        for i in range(max_iter):
            payload = {
                "model":    model,
                "messages": [{"role": "system", "content": system}] + msgs,
                "stream":   True,
                "tools":    tools,
                "options":  options,
            }
            content    = ""
            tool_calls = []

            try:
                # Streaming via httpx direct (get_ollama_client ne gère pas le streaming SSE)
                async with self._http.stream("POST", f"{CFG.OLLAMA}/api/chat",
                                             json=payload) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        m = chunk.get("message", {})
                        if m.get("content"):
                            content += m["content"]
                            # Streaming token-par-token
                            await self.redis.publish(CFG.CH_STREAM, json.dumps(
                                {"session_id": sid, "token": m["content"]}
                            ))
                        for tc in m.get("tool_calls", []):
                            fn = tc.get("function", {})
                            tool_calls.append({
                                "name":      fn.get("name", ""),
                                "arguments": fn.get("arguments", {}),
                            })
            except CircuitOpenError as e:
                log.error("llm_circuit_open", iter=i, session_id=sid, error=str(e))
                return f"Service IA temporairement indisponible: {e}", all_tool_calls
            except Exception as e:
                log.error("llm_error", iter=i, session_id=sid, error=str(e))
                if content:
                    return content.strip(), all_tool_calls
                return f"Erreur de connexion au modèle: {e}", all_tool_calls

            # Pas d'outils → réponse finale
            if not tool_calls:
                return content.strip() or "Réponse vide du modèle.", all_tool_calls

            # Accumuler les tool_calls pour la mémoire procédurale
            all_tool_calls.extend(tool_calls)

            # Exécution parallèle des outils
            names = [tc["name"] for tc in tool_calls]
            log.info("tool_calls_executing", iter=i + 1, tools=names, session_id=sid)
            results = await registry.execute_parallel(tool_calls)

            # Réinjecter dans le contexte
            if content:
                msgs.append({
                    "role":       "assistant",
                    "content":    content,
                    "tool_calls": tool_calls,
                })
            for tc, res in zip(tool_calls, results):
                msgs.append({
                    "role":    "tool",
                    "content": json.dumps(res, ensure_ascii=False)[:4000],
                })

        return content.strip() or "Impossible de terminer après plusieurs tentatives.", all_tool_calls

    # ── Historique Redis TTL 2h ────────────────────────────────
    async def _history_get(self, sid: str) -> list:
        d = await self.redis.get(f"ruche:session:{sid}")
        return json.loads(d)[-20:] if d else []

    async def _history_set(self, sid: str, user: str, assistant: str):
        h  = await self._history_get(sid)
        h += [{"role": "user",      "content": user},
              {"role": "assistant", "content": assistant}]
        await self.redis.setex(f"ruche:session:{sid}", 7200, json.dumps(h))

    # ── Arrêt propre ──────────────────────────────────────────
    async def stop(self):
        await self.router.close()
        await self.memory.close()
        await self._http.aclose()
