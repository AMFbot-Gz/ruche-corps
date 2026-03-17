"""
missions/planner.py — Décomposeur HTN de La Ruche

Prend une mission complexe et la découpe en tâches atomiques
exécutables une par une, avec cache et re-planification sur échec.
"""
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from config import CFG

PLANS_FILE = Path.home() / ".ruche" / "plans.jsonl"
PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)


# ─── Prompt de décomposition ─────────────────────────────────
_DECOMPOSE_PROMPT = """\
Tu es un planificateur expert pour un agent IA autonome sur macOS.
Décompose la mission suivante en étapes ATOMIQUES et EXÉCUTABLES.

MISSION : {mission}

CONTEXTE : {context}

Outils disponibles dans l'agent : {tools}

Retourne UNIQUEMENT du JSON valide (pas de markdown) :
{{
  "goal": "objectif final clair en 1 phrase",
  "complexity": "simple|moderate|complex",
  "estimated_minutes": <int>,
  "tasks": [
    {{
      "id": "t1",
      "description": "action précise et concrète",
      "tool_hint": "nom_de_l_outil_principal_ou_null",
      "depends_on": [],
      "checkpoint": "comment vérifier que cette tâche est réussie"
    }}
  ]
}}

RÈGLES :
- Chaque tâche doit être faisable avec UN appel d'outil
- Ordonner logiquement (exploration → implémentation → test → rapport)
- Maximum 20 tâches pour une mission complex
- Utiliser des depends_on pour les dépendances critiques
- Être SPÉCIFIQUE : pas "faire X" mais "exécuter Y pour obtenir Z"
"""


def _plan_id(mission: str) -> str:
    return "plan_" + hashlib.sha256(mission.encode()).hexdigest()[:10]


def _load_plans() -> dict:
    plans = {}
    if not PLANS_FILE.exists():
        return plans
    for line in PLANS_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                p = json.loads(line)
                plans[p["id"]] = p
            except Exception:
                pass
    return plans


def _save_plan(plan: dict):
    plans = _load_plans()
    plans[plan["id"]] = plan
    with open(PLANS_FILE, "w") as f:
        for p in plans.values():
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


async def decompose(
    mission: str,
    context: str = "",
    tools: list = None,
    force: bool = False,
) -> dict:
    """
    Décompose une mission en plan HTN via Nemotron.
    Retourne le plan (avec cache si déjà fait).
    """
    pid    = _plan_id(mission)
    plans  = _load_plans()

    if not force and pid in plans and plans[pid]["status"] == "done":
        return plans[pid]

    tool_names = ", ".join(tools or [])
    prompt     = _DECOMPOSE_PROMPT.format(
        mission=mission,
        context=context or "Aucun contexte supplémentaire.",
        tools=tool_names,
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            resp = await c.post(
                f"{CFG.OLLAMA}/api/chat",
                json={
                    "model": CFG.M_GENERAL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {
                        "temperature": 0.2,
                        "num_predict": 1500,
                        "num_ctx": CFG.NEMOTRON_CTX,
                    },
                },
            )
        raw  = resp.json().get("message", {}).get("content", "{}")
        m    = re.search(r'\{[\s\S]*\}', raw)
        data = json.loads(m.group()) if m else {}
    except Exception as e:
        print(f"[Planner] Erreur LLM: {e}")
        data = {}

    tasks = data.get("tasks", [])
    if not tasks:
        tasks = [{"id": "t1", "description": mission, "tool_hint": None,
                  "depends_on": [], "checkpoint": "mission terminée"}]

    # Enrichit les tâches
    for i, t in enumerate(tasks):
        t.setdefault("id",         f"t{i+1}")
        t.setdefault("status",     "pending")
        t.setdefault("result",     None)
        t.setdefault("error",      None)
        t.setdefault("retries",    0)
        t.setdefault("depends_on", [])
        t.setdefault("checkpoint", "")
        t.setdefault("tool_hint",  None)
        t.setdefault("started_at", None)
        t.setdefault("done_at",    None)

    plan = {
        "id":                pid,
        "mission":           mission,
        "goal":              data.get("goal", mission),
        "complexity":        data.get("complexity", "moderate"),
        "estimated_minutes": data.get("estimated_minutes", 5),
        "tasks":             tasks,
        "status":            "pending",
        "created_at":        datetime.now().isoformat(),
        "updated_at":        datetime.now().isoformat(),
        "progress":          0,
        "errors":            0,
    }
    _save_plan(plan)
    return plan


def get_plan(plan_id: str) -> Optional[dict]:
    return _load_plans().get(plan_id)


def update_plan(plan: dict):
    plan["updated_at"] = datetime.now().isoformat()
    done  = sum(1 for t in plan["tasks"] if t["status"] == "done")
    total = len(plan["tasks"])
    plan["progress"] = round(done / total * 100) if total else 0
    _save_plan(plan)


def replan_task(plan: dict, task_id: str, error: str) -> dict:
    """Marque une tâche comme failed et propose une description alternative."""
    for t in plan["tasks"]:
        if t["id"] == task_id:
            t["status"]  = "failed"
            t["error"]   = error
            t["retries"] = t.get("retries", 0) + 1
    update_plan(plan)
    return plan


async def replan_task_llm(plan: dict, task_id: str, error: str) -> dict:
    """
    Version async qui demande à Nemotron une description alternative pour la tâche.

    Prompt court: "Tâche {description} échouée: {error}. Alternative en 1 phrase?"
    Met à jour task["description"] avec la réponse LLM.
    Met task["status"] = "pending" pour retry.
    Retourne le plan modifié.
    """
    # Trouver la tâche concernée
    task = next((t for t in plan["tasks"] if t["id"] == task_id), None)
    if not task:
        return plan

    prompt = (
        f"Tâche '{task['description']}' échouée: {error}. "
        f"Propose une approche alternative en 1 phrase (sois très concis)."
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            resp = await c.post(
                f"{CFG.OLLAMA}/api/chat",
                json={
                    "model":    CFG.M_GENERAL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream":   False,
                    "options":  {
                        "temperature": 0.4,
                        "num_predict": 100,
                        "num_ctx":     16384,
                    },
                },
            )
        alternative = resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"[Planner] replan_task_llm erreur: {e}")
        alternative = ""

    if alternative:
        task["description"] = alternative[:200]

    task["status"]  = "pending"
    task["error"]   = error
    task["retries"] = task.get("retries", 0) + 1
    update_plan(plan)
    return plan
