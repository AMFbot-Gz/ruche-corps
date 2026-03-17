"""
Intégration N8N — automatisation de workflows
N8N tourne sur localhost:5678 (n8n-openclaw)
Auth: header 'X-N8N-API-KEY' requis (configuré dans CFG.N8N_API_KEY).
"""
import json
import httpx

from config import CFG

BASE = CFG.N8N_URL  # http://localhost:5678


def _headers() -> dict:
    """Retourne les headers d'auth N8N."""
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if CFG.N8N_API_KEY:
        h["X-N8N-API-KEY"] = CFG.N8N_API_KEY
    return h


async def list_workflows() -> list[dict]:
    """
    Liste tous les workflows N8N avec leur statut (actif/inactif).
    Retourne: [{id, name, active, updatedAt}]
    """
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/v1/workflows", headers=_headers())
        r.raise_for_status()
        data = r.json()
    workflows = data.get("data", data) if isinstance(data, dict) else data
    return [
        {
            "id":        str(w.get("id", "")),
            "name":      w.get("name", ""),
            "active":    w.get("active", False),
            "updatedAt": w.get("updatedAt", ""),
        }
        for w in (workflows if isinstance(workflows, list) else [])
    ]


async def trigger_workflow(workflow_id: str, data: dict = {}) -> dict:
    """
    Déclenche un workflow N8N via l'API d'exécution.
    Retourne la réponse d'exécution.
    """
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/v1/workflows/{workflow_id}/activate",
            headers=_headers(),
            json=data,
        )
        if r.status_code == 404:
            # Essayer la route d'exécution directe
            r = await c.post(
                f"{BASE}/api/v1/executions",
                headers=_headers(),
                json={"workflowId": workflow_id, "data": data},
            )
        r.raise_for_status()
        return r.json()


async def get_executions(workflow_id: str = "", limit: int = 5) -> list[dict]:
    """
    Retourne les dernières exécutions d'un workflow.
    workflow_id: filtrer par workflow (vide = toutes)
    """
    params = {"limit": limit}
    if workflow_id:
        params["workflowId"] = workflow_id

    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(
            f"{BASE}/api/v1/executions",
            headers=_headers(),
            params=params,
        )
        r.raise_for_status()
        data = r.json()

    items = data.get("data", data) if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


async def create_webhook_workflow(name: str, webhook_path: str) -> dict:
    """
    Crée un workflow simple avec un trigger webhook.
    Retourne le workflow créé (id, name, webhookUrl).
    """
    payload = {
        "name": name,
        "nodes": [
            {
                "id":         "webhook-trigger",
                "name":       "Webhook",
                "type":       "n8n-nodes-base.webhook",
                "typeVersion": 1,
                "position":   [250, 300],
                "parameters": {
                    "path":           webhook_path,
                    "responseMode":   "onReceived",
                    "responseData":   "allEntries",
                },
            }
        ],
        "connections": {},
        "settings":    {"executionOrder": "v1"},
        "active":      False,
    }
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            f"{BASE}/api/v1/workflows",
            headers=_headers(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()
