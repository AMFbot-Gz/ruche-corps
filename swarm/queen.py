"""
swarm/queen.py — Queen coordinatrice du swarm

La Queen:
1. Reçoit une tâche complexe
2. Analyse et décompose en sous-tâches
3. Assigne chaque sous-tâche au bon spécialiste
4. Lance les spécialistes EN PARALLÈLE si possible
5. Agrège les résultats
6. Synthèse finale via M_GENERAL (Nemotron)

Jamais d'exécution directe — uniquement coordination.
"""
import asyncio
import json
import re
import time

import httpx

from config import CFG
from core.logger import get_logger

log = get_logger(__name__)

# Import des spécialistes (chargés une seule fois au niveau module)
from swarm.specialists import SPECIALISTS

QUEEN_DECOMPOSE_PROMPT = """\
Tu es la Queen, coordinatrice d'un swarm d'agents spécialistes.

TÂCHE: {task}

CONTEXTE ADDITIONNEL: {context}

Spécialistes disponibles:
- code: édition de code, Python, shell, analyse, aider
- web: recherche web, fetch URLs, APIs
- file: lecture/écriture/organisation de fichiers
- memory: mémorisation, recall, résumés de session
- computer: contrôle macOS, screenshots, click, type

Décompose en sous-tâches assignées aux bons spécialistes.
JSON uniquement, sans texte autour:
{{
  "can_parallelize": true,
  "subtasks": [
    {{"specialist": "code", "task": "description précise", "depends_on": []}},
    {{"specialist": "web",  "task": "description précise", "depends_on": [0]}}
  ]
}}

Règles:
- depends_on: liste d'indices (0-based) des sous-tâches dont cette sous-tâche dépend
- Si une sous-tâche n'a pas de dépendance, depends_on = []
- can_parallelize = true si au moins 2 sous-tâches peuvent s'exécuter en même temps
- Minimise le nombre de sous-tâches (1 si la tâche est simple)
"""


class Queen:
    """Coordinatrice du swarm d'agents spécialistes."""

    async def execute(self, task: str, context: str = "") -> str:
        """
        Coordonne l'exécution d'une tâche complexe via le swarm.

        1. Décompose en sous-tâches (via M_FAST)
        2. Si can_parallelize: asyncio.gather() sur les sous-tâches indépendantes
        3. Sinon: exécution séquentielle avec résultat précédent comme contexte
        4. Synthèse finale (via M_GENERAL)
        5. Retourne résultat agrégé
        """
        t0 = time.monotonic()
        log.info("queen_execute", task_preview=task[:80])

        # Décomposition
        try:
            plan = await self._decompose(task, context)
        except Exception as e:
            log.error("queen_decompose_error", error=str(e))
            # Fallback: exécuter directement avec le code_agent
            return await self._fallback_single(task, context)

        # Sécurité : plan invalide ou sans sous-tâches
        if not plan or "subtasks" not in plan:
            log.warning("queen_decompose_failed", raw=str(plan)[:200])
            # Fallback: exécuter la tâche entière avec le spécialiste le plus généraliste
            return await SPECIALISTS["file"].execute(task, context)

        subtasks = plan.get("subtasks", [])
        if not subtasks:
            log.warning("queen_empty_plan", task_preview=task[:80])
            return await self._fallback_single(task, context)

        log.info("queen_plan", n_subtasks=len(subtasks),
                 can_parallelize=plan.get("can_parallelize", False))

        # Exécution du plan
        results: list[str | None] = [None] * len(subtasks)

        if plan.get("can_parallelize", False):
            # Exécution avec respect des dépendances
            results = await self._execute_with_deps(subtasks, results)
        else:
            # Exécution séquentielle simple
            for idx, subtask in enumerate(subtasks):
                prior = [r for r in results[:idx] if r is not None]
                prior_ctx = "\n\n".join(prior) if prior else ""
                try:
                    results[idx] = await self._execute_subtask(subtask, list(filter(None, results[:idx])))
                except Exception as e:
                    log.error("queen_subtask_error",
                              specialist=subtask.get("specialist"),
                              idx=idx, error=str(e))
                    results[idx] = f"[Erreur spécialiste {subtask.get('specialist', '?')}: {e}]"

        # Filtrer les résultats None
        valid_results = [r for r in results if r is not None]

        if not valid_results:
            return "Aucun spécialiste n'a pu produire de résultat."

        # Synthèse finale
        if len(valid_results) == 1:
            final = valid_results[0]
        else:
            final = await self._synthesize(task, valid_results)

        ms = (time.monotonic() - t0) * 1000
        log.info("queen_done", ms=round(ms, 1), n_results=len(valid_results))
        return final

    async def _execute_with_deps(self, subtasks: list, results: list) -> list:
        """
        Exécute les sous-tâches en respectant les dépendances.
        Les sous-tâches sans dépendances s'exécutent en parallèle.
        """
        n = len(subtasks)
        completed = [False] * n

        # Répéter jusqu'à ce que toutes les tâches soient complétées
        max_rounds = n + 1
        for _ in range(max_rounds):
            # Trouver les tâches prêtes (dépendances satisfaites, pas encore complétées)
            ready = [
                idx for idx, st in enumerate(subtasks)
                if not completed[idx]
                and all(completed[dep] for dep in st.get("depends_on", []))
            ]
            if not ready:
                break  # Toutes complétées ou blocage circulaire

            # Exécuter les tâches prêtes en parallèle
            async def run_one(idx: int) -> tuple[int, str]:
                subtask   = subtasks[idx]
                prior     = [results[dep] for dep in subtask.get("depends_on", [])
                             if results[dep] is not None]
                try:
                    r = await self._execute_subtask(subtask, prior)
                except Exception as e:
                    log.error("queen_subtask_error",
                              specialist=subtask.get("specialist"),
                              idx=idx, error=str(e))
                    r = f"[Erreur spécialiste {subtask.get('specialist', '?')}: {e}]"
                return idx, r

            batch = await asyncio.gather(*[run_one(i) for i in ready])
            for idx, r in batch:
                results[idx]   = r
                completed[idx] = True

        return results

    async def _decompose(self, task: str, context: str) -> dict:
        """Appelle M_FAST pour décomposer en sous-tâches JSON."""
        prompt = QUEEN_DECOMPOSE_PROMPT.format(
            task=task,
            context=context or "(aucun contexte additionnel)",
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(f"{CFG.OLLAMA}/api/chat", json={
                    "model":    CFG.M_FAST,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream":   False,
                    "options":  {"temperature": 0.3, "num_predict": 800},
                })
                r.raise_for_status()
                content = r.json().get("message", {}).get("content", "")
        except Exception as e:
            raise RuntimeError(f"Échec décomposition Queen: {e}") from e

        # Parse JSON robuste : chercher le premier bloc JSON dans la réponse
        plan = _extract_json(content)
        if plan is None:
            raise ValueError(f"JSON invalide dans la réponse de décomposition: {content[:200]}")

        return plan

    async def _execute_subtask(self, subtask: dict, prior_results: list[str]) -> str:
        """Exécute une sous-tâche via le bon spécialiste."""
        specialist_key = subtask.get("specialist", "code")
        task_desc      = subtask.get("task", "")

        agent = SPECIALISTS.get(specialist_key)
        if agent is None:
            log.warning("queen_unknown_specialist", specialist=specialist_key)
            # Fallback vers code_agent
            agent = SPECIALISTS["code"]

        # Construire le contexte depuis les résultats précédents
        context = "\n\n".join(prior_results) if prior_results else ""
        return await agent.execute(task_desc, context=context)

    async def _synthesize(self, task: str, results: list[str]) -> str:
        """Synthèse Nemotron (M_GENERAL) des résultats de tous les spécialistes."""
        labeled = "\n\n".join(
            f"**Résultat {i+1}:**\n{r}"
            for i, r in enumerate(results)
        )
        synth_prompt = (
            f"Voici les résultats de {len(results)} agents spécialistes "
            f"sur la tâche: '{task}'\n\n"
            f"{labeled[:4000]}\n\n"
            "Synthétise ces résultats en une réponse finale claire, complète et cohérente. "
            "Élimine les redondances, conserve tous les faits importants."
        )

        is_nemotron = "nemotron" in CFG.M_GENERAL.lower()
        options: dict = {
            "temperature": CFG.NEMOTRON_TEMP if is_nemotron else 0.5,
            "num_predict": 1000,
        }
        if is_nemotron:
            options["num_ctx"] = CFG.NEMOTRON_CTX

        try:
            async with httpx.AsyncClient(timeout=120.0) as c:
                r = await c.post(f"{CFG.OLLAMA}/api/chat", json={
                    "model":    CFG.M_GENERAL,
                    "messages": [{"role": "user", "content": synth_prompt}],
                    "stream":   False,
                    "options":  options,
                })
                r.raise_for_status()
                return r.json().get("message", {}).get("content", labeled)
        except Exception as e:
            log.error("queen_synthesize_error", error=str(e))
            # Fallback: concaténation simple
            return labeled

    async def _fallback_single(self, task: str, context: str) -> str:
        """Fallback: déléguer directement au code_agent si la décomposition échoue."""
        log.info("queen_fallback_single", task_preview=task[:60])
        agent = SPECIALISTS.get("code", list(SPECIALISTS.values())[0])
        return await agent.execute(task, context=context)


# ─── Extraction JSON robuste ────────────────────────────────────────────────
def _extract_json(text: str) -> dict | None:
    """
    Extrait le premier objet JSON valide depuis un texte potentiellement bruité.
    Gère les blocs ```json ... ```, le JSON inline, et les réponses partielles.
    """
    # 1. Chercher un bloc ```json ... ```
    md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if md_match:
        try:
            return json.loads(md_match.group(1))
        except json.JSONDecodeError:
            pass

    # 2. Chercher le premier { ... } complet (approche greedy)
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break

    # 3. Tentative de repair minimal : retirer les commentaires JS-style
    cleaned = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    start   = cleaned.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(cleaned[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start:i + 1])
                    except json.JSONDecodeError:
                        break

    return None


# ─── Singleton Queen ────────────────────────────────────────────────────────
_queen: Queen | None = None


def get_queen() -> Queen:
    """Retourne l'instance singleton de la Queen."""
    global _queen
    if _queen is None:
        _queen = Queen()
    return _queen
