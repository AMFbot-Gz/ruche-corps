"""
swarm/base.py — Agent spécialiste de base

Chaque spécialiste:
- A un rôle unique et un set d'outils limité
- Maintient son propre historique de conversation (contexte propre)
- Mémorise ses séquences réussies (mémoire procédurale)
- Communique via Redis avec la Queen
"""
import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from config import CFG
from core.logger import get_logger

log = get_logger(__name__)

# Importation différée du registry pour éviter les imports circulaires
# Le registry est peuplé par tools.builtins lors de l'import


@dataclass
class ExecutionResult:
    """Résultat d'exécution d'une tâche par un spécialiste."""
    specialist: str
    task: str
    result: str
    iterations: int
    duration_ms: float
    success: bool


class SpecialistAgent:
    """
    Agent spécialiste avec contexte isolé.

    Attributs:
        name: str — nom du spécialiste (ex: "code_agent")
        role: str — description du rôle (injectée dans system prompt)
        allowed_tools: list[str] — noms des outils autorisés SEULEMENT
        model: str — modèle à utiliser (défaut: CFG.M_GENERAL)
        max_iter: int — max iterations ReAct (défaut: 8)
    """

    def __init__(
        self,
        name: str,
        role: str,
        allowed_tools: list,
        model: str = "",
        max_iter: int = 8,
    ):
        self.name          = name
        self.role          = role
        self.allowed_tools = allowed_tools
        self.model         = model or CFG.M_GENERAL
        self.max_iter      = max_iter

        # Historique de conversation propre à ce spécialiste (contexte isolé)
        self._history: list[dict] = []

        # Mémoire procédurale : séquences d'outils réussies
        self._successful_sequences: list[list[str]] = []

    # ── API publique ──────────────────────────────────────────────────

    async def execute(self, task: str, context: str = "") -> str:
        """
        Exécute une tâche avec ses outils dédiés.

        Boucle ReAct jusqu'à max_iter ou réponse finale.
        Retourne le résultat + métriques.
        """
        t0 = time.monotonic()
        log.info("specialist_execute", specialist=self.name, task_preview=task[:80])

        system   = self._build_system()
        schemas  = await self._get_allowed_schemas()

        # Contexte additionnel si fourni
        user_msg = task
        if context:
            user_msg = f"CONTEXTE DES ÉTAPES PRÉCÉDENTES:\n{context}\n\nTÂCHE:\n{task}"

        messages = self._history.copy() + [{"role": "user", "content": user_msg}]

        result       = ""
        tool_sequence: list[str] = []

        for i in range(self.max_iter):
            content, tool_calls = await self._call_llm(
                [{"role": "system", "content": system}] + messages,
                schemas,
            )

            # Pas d'outils → réponse finale
            if not tool_calls:
                result = content.strip() or "Tâche terminée sans sortie textuelle."
                break

            # Enregistrer les noms d'outils pour la mémoire procédurale
            names = [tc["name"] for tc in tool_calls]
            tool_sequence.extend(names)
            log.info("specialist_tools", specialist=self.name, iter=i + 1, tools=names)

            # Exécution parallèle des outils via le registry
            from tools.registry import registry
            exec_results = await registry.execute_parallel(tool_calls)

            # Réinjecter dans le contexte
            if content:
                messages.append({
                    "role":       "assistant",
                    "content":    content,
                    "tool_calls": tool_calls,
                })
            for tc, res in zip(tool_calls, exec_results):
                messages.append({
                    "role":    "tool",
                    "content": json.dumps(res, ensure_ascii=False)[:4000],
                })

            # Si erreur dans tous les outils, stopper
            all_errors = all("error" in r for r in exec_results)
            if all_errors and i > 0:
                result = content.strip() or f"Échec des outils: {exec_results}"
                break

        else:
            result = content.strip() if content else "Max iterations atteintes sans réponse finale."

        # Mémoriser la séquence si elle a produit un résultat non-vide
        if tool_sequence and result and "Erreur" not in result[:20]:
            self._successful_sequences.append(tool_sequence)
            # Garder les 20 dernières séquences
            self._successful_sequences = self._successful_sequences[-20:]

        # Mise à jour historique (contexte propre limité à 10 échanges)
        self._history.append({"role": "user",      "content": user_msg})
        self._history.append({"role": "assistant", "content": result})
        self._history = self._history[-20:]  # 10 échanges max

        ms = (time.monotonic() - t0) * 1000
        log.info("specialist_done", specialist=self.name, ms=round(ms, 1),
                 result_preview=result[:60])
        return result

    def get_successful_sequences(self) -> list[list[str]]:
        """Retourne les séquences d'outils réussies (mémoire procédurale)."""
        return self._successful_sequences.copy()

    def clear_history(self):
        """Réinitialise le contexte de conversation."""
        self._history = []

    # ── Méthodes internes ─────────────────────────────────────────────

    async def _call_llm(self, messages: list, tools: list) -> tuple[str, list]:
        """
        Appel Ollama avec les outils du spécialiste.
        Utilise httpx async context manager (pas de leak).

        Retourne (content: str, tool_calls: list).
        """
        # Options selon le modèle
        is_nemotron = "nemotron" in self.model.lower()
        options: dict = {
            "temperature": CFG.NEMOTRON_TEMP if is_nemotron else 0.7,
            "num_predict": 2000,
        }
        if is_nemotron:
            options["num_ctx"] = CFG.NEMOTRON_CTX

        payload = {
            "model":    self.model,
            "messages": messages,
            "stream":   False,
            "tools":    tools,
            "options":  options,
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as c:
                r = await c.post(f"{CFG.OLLAMA}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()

            msg        = data.get("message", {})
            content    = msg.get("content", "")
            tool_calls = []

            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                tool_calls.append({
                    "name":      fn.get("name", ""),
                    "arguments": fn.get("arguments", {}),
                })

            return content, tool_calls

        except httpx.HTTPStatusError as e:
            log.error("specialist_llm_http_error",
                      specialist=self.name, status=e.response.status_code)
            return f"Erreur HTTP {e.response.status_code} lors de l'appel au modèle.", []
        except Exception as e:
            log.error("specialist_llm_error", specialist=self.name, error=str(e))
            return f"Erreur de connexion au modèle: {e}", []

    def _build_system(self) -> str:
        """
        Construit le system prompt spécifique au spécialiste.
        Inclut: rôle, outils disponibles, règles spécifiques.
        """
        # Séquences réussies pour guider le modèle (mémoire procédurale)
        proc_mem = ""
        if self._successful_sequences:
            last = self._successful_sequences[-3:]
            seq_str = "\n".join(f"  • {' → '.join(s)}" for s in last)
            proc_mem = f"\nSÉQUENCES RÉUSSIES PRÉCÉDENTES (à réutiliser si pertinent):\n{seq_str}\n"

        tools_str = ", ".join(self.allowed_tools)

        return f"""\
Tu es {self.name} — {self.role}

OUTILS DISPONIBLES (SEULEMENT CES OUTILS) : {tools_str}

RÈGLES :
- N'utilise QUE les outils listés ci-dessus
- Agis directement, pas de confirmation demandée
- Si un outil échoue, essaie une approche alternative avec tes outils disponibles
- Réponds de façon concise une fois la tâche terminée
- "Je ne peux pas" n'existe pas — trouve le chemin avec tes outils
{proc_mem}"""

    async def _get_allowed_schemas(self) -> list:
        """
        Retourne les schémas JSON des outils autorisés seulement.
        Via tools.registry.registry.get_schemas() filtré.
        """
        # Import différé pour s'assurer que builtins est chargé
        import tools.builtins  # noqa: F401 — déclenche l'enregistrement @tool
        from tools.registry import registry

        all_schemas    = registry.get_schemas()
        allowed_set    = set(self.allowed_tools)
        filtered       = [
            s for s in all_schemas
            if s.get("function", {}).get("name", "") in allowed_set
        ]

        if not filtered:
            # Fallback: retourner tous les schemas si aucun outil trouvé
            log.warning("specialist_no_tools_found",
                        specialist=self.name,
                        allowed=self.allowed_tools,
                        available=registry.list_tools())
            return all_schemas

        return filtered
