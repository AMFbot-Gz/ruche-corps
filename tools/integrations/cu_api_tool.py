"""
Intégration Computer Use API — port 8015
API locale FastAPI avec sessions, screenshots, contrôle d'affichage.
mode=anthropic, model=claude-opus-4-6, display=1536×960

Endpoints confirmés:
  POST /screenshot       — prend un screenshot (body: {label: str})
  POST /session/start    — démarre une session (body: {goal, max_steps, mode})
  GET  /session/{id}     — statut d'une session
  POST /session/{id}/stop — arrête une session
  GET  /sessions         — liste des sessions
  GET  /stats            — statistiques
  GET  /health           — santé + infos
  GET  /display          — infos écran
"""
import httpx

from config import CFG

BASE = CFG.CU_API_URL  # http://localhost:8015


async def cu_screenshot(label: str = "") -> dict:
    """
    Prend un screenshot via l'API CU.
    Retourne {hash, base64, resolution, retina}.
    """
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(f"{BASE}/screenshot", json={"label": label})
        r.raise_for_status()
        return r.json()


async def cu_start_session(task: str = "", max_steps: int = 20) -> str:
    """
    Démarre une session Computer Use.
    Retourne le session_id.
    """
    payload = {
        "goal":      task,
        "max_steps": max_steps,
        "mode":      "anthropic",
    }
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(f"{BASE}/session/start", json=payload)
        r.raise_for_status()
        data = r.json()

    # Extraire l'id selon la structure de réponse
    return str(data.get("session_id") or data.get("id") or data.get("sessionId") or "")


async def cu_get_session(session_id: str) -> dict:
    """Retourne le statut d'une session (étapes, résultat, statut)."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{BASE}/session/{session_id}")
        r.raise_for_status()
        return r.json()


async def cu_stop_session(session_id: str) -> dict:
    """Arrête une session Computer Use."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{BASE}/session/{session_id}/stop")
        r.raise_for_status()
        return r.json()


async def cu_list_sessions() -> list[dict]:
    """Liste toutes les sessions actives et récentes."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{BASE}/sessions")
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else data.get("sessions", [])


async def cu_get_status() -> dict:
    """
    Statut complet de l'API Computer Use.
    Combine /health et /display.
    Retourne: {status, mode, cu_model, display, retina, active_sessions, ...}
    """
    async with httpx.AsyncClient(timeout=10.0) as c:
        health_r  = await c.get(f"{BASE}/health")
        display_r = await c.get(f"{BASE}/display")

    health  = health_r.json()
    display = display_r.json()

    return {**health, "display_info": display}
