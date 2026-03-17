"""
missions/executor.py — Exécuteur autonome de La Ruche

Prend un plan décomposé et exécute chaque tâche une par une,
avec récupération d'erreur, re-planification, et rapport de progression.

Peut tourner toute la nuit sans supervision.
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Optional

import httpx
import redis.asyncio as aioredis

from config import CFG
from core.logger import get_logger
from missions.planner import update_plan, replan_task
from tools.registry import registry

log = get_logger("executor")

# Nombre max de tentatives par tâche avant abandon
MAX_RETRIES    = 2
# Pause entre tâches (laisser respirer le système)
TASK_PAUSE_SEC = 1.5
# Pause entre tentatives en cas d'échec
RETRY_PAUSE_SEC = 4.0


class MissionExecutor:
    """
    Exécute un plan tâche par tâche, de façon autonome et résiliente.

    Stratégie Claude :
    1. Pour chaque tâche : construire un prompt ciblé + appeler le bon outil
    2. Vérifier le résultat via le checkpoint
    3. En cas d'échec : retry avec contexte d'erreur → replan si épuisé
    4. Publier le progrès sur Redis (pour Telegram + heartbeat)
    5. Sauvegarder l'état après chaque tâche (résistant aux crashes)
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client
        # Pas de client httpx persistant — on crée un context manager par appel
        # pour éviter les fuites de connexions (httpx leak fix)

    async def run(self, plan: dict, report_every: int = 3) -> dict:
        """
        Execute le plan complet. Retourne le plan finalisé.

        plan:         plan HTN (de planner.py)
        report_every: envoyer un rapport Telegram tous les N tâches
        """
        plan["status"]     = "executing"
        plan["started_at"] = datetime.now().isoformat()
        update_plan(plan)

        await self._report(f"🚀 Mission démarrée : **{plan['goal']}**\n"
                           f"{len(plan['tasks'])} tâches · ~{plan.get('estimated_minutes',5)} min")

        done_count = 0
        ctx_summary = []  # résumés des tâches précédentes (contexte glissant)

        for task in plan["tasks"]:
            # Skip si déjà fait (reprise après crash)
            if task["status"] == "done":
                done_count += 1
                continue

            # Vérifier dépendances
            if not self._deps_ok(plan, task):
                task["status"] = "skipped"
                task["result"] = "Dépendances non satisfaites"
                update_plan(plan)
                continue

            # Exécuter avec retries
            success = False
            for attempt in range(MAX_RETRIES + 1):
                task["status"]     = "running"
                task["started_at"] = datetime.now().isoformat()
                update_plan(plan)

                result = await self._execute_task(task, ctx_summary, attempt)
                task["result"]  = result
                task["done_at"] = datetime.now().isoformat()

                if result and not result.startswith("ERREUR"):
                    task["status"] = "done"
                    done_count += 1
                    success = True
                    ctx_summary.append(f"[{task['id']}] {task['description']}: {result[:120]}")
                    if len(ctx_summary) > 6:
                        ctx_summary = ctx_summary[-6:]  # garder les 6 derniers
                    break
                else:
                    task["retries"] = attempt + 1
                    task["error"]   = result
                    if attempt < MAX_RETRIES:
                        print(f"[Executor] ⚠️  Tâche {task['id']} échouée (tentative {attempt+1}) — retry dans {RETRY_PAUSE_SEC}s")
                        await asyncio.sleep(RETRY_PAUSE_SEC)
                    else:
                        # MAX_RETRIES épuisés → replanification intelligente
                        replanned = await self._smart_replan(plan, task)
                        if not replanned:
                            task["status"] = "failed"
                            plan["errors"] = plan.get("errors", 0) + 1

            update_plan(plan)

            # Rapport de progression tous les N tâches
            if done_count > 0 and done_count % report_every == 0:
                pct = round(done_count / len(plan["tasks"]) * 100)
                await self._report(
                    f"⚙️ Progression **{plan['goal'][:50]}**\n"
                    f"{done_count}/{len(plan['tasks'])} tâches · {pct}% ✅"
                )

            await asyncio.sleep(TASK_PAUSE_SEC)

        # Finalisation
        total  = len(plan["tasks"])
        failed = sum(1 for t in plan["tasks"] if t["status"] == "failed")
        skipped = sum(1 for t in plan["tasks"] if t["status"] == "skipped")
        plan["status"]    = "done" if failed == 0 else ("partial" if done_count > 0 else "failed")
        plan["done_at"]   = datetime.now().isoformat()
        update_plan(plan)

        # Rapport final
        status_emoji = "✅" if plan["status"] == "done" else ("⚠️" if plan["status"] == "partial" else "❌")
        final_report = (
            f"{status_emoji} Mission terminée : **{plan['goal']}**\n"
            f"✅ {done_count} réussies · ❌ {failed} échouées · ⏭ {skipped} sautées\n"
        )
        if done_count > 0:
            final_report += "\n**Résumé :**\n" + "\n".join(f"• {s}" for s in ctx_summary[-5:])
        await self._report(final_report)

        return plan

    def _deps_ok(self, plan: dict, task: dict) -> bool:
        """Vérifie que toutes les tâches dont dépend celle-ci sont terminées."""
        deps = task.get("depends_on", [])
        if not deps:
            return True
        done_ids = {t["id"] for t in plan["tasks"] if t["status"] == "done"}
        return all(d in done_ids for d in deps)

    async def _execute_task(self, task: dict, ctx_summary: list, attempt: int) -> str:
        """
        Exécute une tâche atomique via le LLM + outils.
        Retourne le résultat ou "ERREUR: ..."
        """
        ctx_text = "\n".join(ctx_summary[-4:]) if ctx_summary else ""
        retry_note = f"\n⚠️ Tentative {attempt+1} — erreur précédente: {task.get('error','')}" if attempt > 0 else ""

        # Prompt ciblé sur la tâche unique
        prompt = (
            f"Tâche : {task['description']}\n"
            + (f"Outil suggéré : {task['tool_hint']}\n" if task.get('tool_hint') else "")
            + (f"\nContexte des tâches précédentes :\n{ctx_text}\n" if ctx_text else "")
            + retry_note
            + "\nExécute cette tâche avec les outils disponibles. "
            "Sois concis sur le résultat."
        )

        tools   = registry.get_schemas()
        options = {
            "temperature": 0.3,
            "num_predict": 1500,
            "num_ctx":     CFG.NEMOTRON_CTX,
        }
        payload = {
            "model":    CFG.M_GENERAL,
            "messages": [
                {"role": "system", "content":
                    f"Tu es {CFG.NAME}, agent autonome. "
                    "Exécute les tâches qu'on te donne avec les outils fournis. "
                    "Retourne toujours un résultat concret et court."},
                {"role": "user", "content": prompt},
            ],
            "stream":  False,
            "tools":   tools,
            "options": options,
        }

        try:
            content    = ""
            tool_calls = []

            # Chaque appel crée son propre client httpx (évite les fuites de connexions)
            async with httpx.AsyncClient(timeout=180.0) as http:
                async with http.stream("POST", f"{CFG.OLLAMA}/api/chat",
                                       json=payload) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        m = chunk.get("message", {})
                        content += m.get("content", "")
                        for tc in m.get("tool_calls", []):
                            fn = tc.get("function", {})
                            tool_calls.append({
                                "name":      fn.get("name", ""),
                                "arguments": fn.get("arguments", {}),
                            })

            # Vérification post-condition pour chaque outil appelé
            from core.verifier import get_verifier
            verifier = get_verifier()
            for tc in tool_calls:
                try:
                    v_result = await asyncio.wait_for(
                        verifier.verify(tc["name"], tc["arguments"], content),
                        timeout=5.0
                    )
                    if not v_result.success:
                        log.warning("tool_verification_failed",
                                    tool=tc["name"],
                                    reason=v_result.message)
                        # Ajouter l'échec de vérification dans le résultat
                        content = f"ERREUR (vérification): {v_result.message} | LLM disait: {content[:100]}"
                except asyncio.TimeoutError:
                    log.warning("verification_timeout", tool=tc["name"])
                except Exception as e:
                    log.warning("verification_error", tool=tc["name"], error=str(e))

            # Exécuter les outils si demandés
            if tool_calls:
                results = await registry.execute_parallel(tool_calls)
                # Ajouter les résultats et redemander une synthèse
                msgs = payload["messages"] + [
                    {"role": "assistant", "content": content, "tool_calls": tool_calls},
                    *[{"role": "tool", "content": json.dumps(r, ensure_ascii=False)[:3000]}
                      for r in results],
                    {"role": "user", "content": "Résume le résultat de cette tâche en 1-3 phrases."},
                ]
                async with httpx.AsyncClient(timeout=180.0) as http2:
                    async with http2.stream("POST", f"{CFG.OLLAMA}/api/chat",
                                            json={**payload, "messages": msgs, "tools": []}) as resp2:
                        summary = ""
                        async for line in resp2.aiter_lines():
                            if line:
                                try:
                                    summary += json.loads(line).get("message", {}).get("content", "")
                                except Exception:
                                    pass
                return summary.strip() or content.strip() or "OK"

            return content.strip() or "OK (pas d'outil utilisé)"

        except Exception as e:
            return f"ERREUR: {e}"

    async def _smart_replan(self, plan: dict, failed_task: dict) -> bool:
        """
        Demande à Nemotron une approche alternative pour la tâche échouée.
        Retourne True si une nouvelle tâche a été insérée dans le plan, False sinon.

        Prompt: "La tâche '{description}' a échoué avec: '{error}'.
        Propose UNE approche alternative concrète avec un outil différent.
        JSON: {description: str, tool_hint: str, rationale: str}"

        Si succès: insère la nouvelle tâche juste après la tâche échouée dans plan["tasks"]
        """
        error       = failed_task.get("error", "erreur inconnue")
        description = failed_task.get("description", "")

        prompt = (
            f"La tâche '{description}' a échoué avec: '{error}'. "
            f"Propose UNE approche alternative concrète avec un outil différent. "
            f"Réponds UNIQUEMENT en JSON (sans markdown) : "
            f'{{ "description": "...", "tool_hint": "...", "rationale": "..." }}'
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                resp = await c.post(
                    f"{CFG.OLLAMA}/api/chat",
                    json={
                        "model":    CFG.M_GENERAL,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream":   False,
                        "options":  {
                            "temperature": 0.4,
                            "num_predict": 300,
                            "num_ctx":     CFG.NEMOTRON_CTX,
                        },
                    },
                )
            raw  = resp.json().get("message", {}).get("content", "{}")
            import re as _re
            m    = _re.search(r'\{[\s\S]*\}', raw)
            data = json.loads(m.group()) if m else {}
        except Exception as e:
            print(f"[Executor] _smart_replan erreur LLM: {e}")
            return False

        new_desc = data.get("description", "").strip()
        if not new_desc:
            return False

        # Construire la nouvelle tâche
        new_task = {
            "id":          f"{failed_task['id']}_alt",
            "description": new_desc,
            "tool_hint":   data.get("tool_hint", None),
            "depends_on":  failed_task.get("depends_on", []),
            "checkpoint":  f"Alternative à {failed_task['id']}",
            "status":      "pending",
            "result":      None,
            "error":       None,
            "retries":     0,
            "started_at":  None,
            "done_at":     None,
        }

        # Insérer juste après la tâche échouée
        idx = next(
            (i for i, t in enumerate(plan["tasks"]) if t["id"] == failed_task["id"]),
            None,
        )
        if idx is not None:
            plan["tasks"].insert(idx + 1, new_task)
        else:
            plan["tasks"].append(new_task)

        # Marquer la tâche originale comme failed (on a une alternative)
        failed_task["status"] = "failed"
        plan["errors"] = plan.get("errors", 0) + 1

        print(
            f"[Executor] Replanification : nouvelle tâche '{new_task['id']}' insérée "
            f"({data.get('rationale','')[:80]})"
        )
        return True

    async def _report(self, message: str):
        """Publie un rapport sur Redis (→ Telegram + logs)."""
        print(f"[Executor] {message[:120]}")
        if self._redis:
            try:
                await self._redis.publish(CFG.CH_HB, json.dumps({
                    "type":    "mission_report",
                    "level":   "info",
                    "message": message,
                }))
            except Exception:
                pass

    async def close(self):
        """Pas de ressource persistante à fermer (clients httpx créés à la volée)."""
        pass
