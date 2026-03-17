"""
heartbeat.py — Système autonome de La Ruche

Tourne en arrière-plan, surveille, anticipe, agit sans qu'on lui demande.
Comme le système nerveux autonome : le cœur bat sans que le cerveau l'ordonne.
"""
import asyncio
import json
import time
from datetime import datetime

import httpx
import redis.asyncio as aioredis

from config import CFG


class HeartbeatService:
    def __init__(self):
        self._http  = httpx.AsyncClient(timeout=5.0)
        self._redis = None
        self._down  = {}
        self._briefing_done_today = False

    async def start(self, redis_client=None):
        self._redis = redis_client or await aioredis.from_url(CFG.REDIS)
        print("[Heartbeat] Service autonome démarré.")
        await asyncio.gather(
            self._health_loop(),
            self._briefing_loop(),
            self._disk_monitor(),
        )

    # ─── Boucle de santé (60s) ────────────────────────────────────────────
    async def _health_loop(self):
        checks = {
            "Ollama":    f"{CFG.OLLAMA}/api/tags",
            "Ghost OS":  f"{CFG.GHOST_URL}/api/health",
            "Comp. Use": f"{CFG.GHOST_CU}/health",
        }
        while True:
            for name, url in checks.items():
                try:
                    await self._http.get(url)
                    if self._down.get(name):
                        await self._alert(f"{name} est de retour en ligne, {CFG.OWNER}.", "info")
                        self._down[name] = False
                except Exception:
                    if not self._down.get(name):
                        await self._alert(f"⚠️ {name} est tombé, {CFG.OWNER}.", "warn")
                        self._down[name] = True
            await asyncio.sleep(60)

    # ─── Briefing matinal (8h00) ──────────────────────────────────────────
    async def _briefing_loop(self):
        while True:
            now = datetime.now()
            if now.hour == 8 and now.minute < 5 and not self._briefing_done_today:
                await self._morning_briefing()
                self._briefing_done_today = True
            if now.hour == 0:
                self._briefing_done_today = False
            await asyncio.sleep(60)

    async def _morning_briefing(self):
        status_parts = []

        try:
            r = await self._http.get(f"{CFG.GHOST_URL}/api/status")
            data     = r.json()
            missions = data.get("missions", {}).get("total", 0)
            uptime   = data.get("uptime", 0)
            status_parts.append(f"Ghost OS: actif depuis {uptime // 3600}h, {missions} missions totales")
        except Exception:
            status_parts.append("Ghost OS: indisponible")

        try:
            r      = await self._http.get(f"{CFG.OLLAMA}/api/tags")
            models = r.json().get("models", [])
            status_parts.append(f"Ollama: {len(models)} modèles disponibles")
        except Exception:
            status_parts.append("Ollama: indisponible")

        date_str       = datetime.now().strftime("%A %d %B %Y")
        status_context = "\n".join(status_parts)

        prompt = f"""Tu es Jarvis. Il est 8h00 le {date_str}.
Génère un briefing matinal concis (3-4 phrases) pour {CFG.OWNER}.
Mentionne l'état du système et donne un ton positif et motivant.

État du système :
{status_context}

Briefing (voix de Jarvis, ton britannique, appelle "{CFG.OWNER}") :"""

        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                resp = await c.post(
                    f"{CFG.OLLAMA}/api/chat",
                    json={
                        "model": CFG.M_FAST,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.8, "num_predict": 200},
                    },
                )
            briefing = resp.json().get("message", {}).get("content",
                f"Bonjour {CFG.OWNER}, systèmes opérationnels.")
        except Exception:
            briefing = f"Bonjour {CFG.OWNER}, systèmes opérationnels. Bonne journée."

        await self._alert(briefing, "briefing")
        print(f"[Heartbeat] Briefing matinal envoyé: {briefing[:80]}")

    # ─── Surveillance disque (toutes les 5 min) ───────────────────────────
    async def _disk_monitor(self):
        import shutil
        alerted_disk = False
        while True:
            total, used, free = shutil.disk_usage("/")
            free_gb = free // (1024 ** 3)
            if free_gb < 10 and not alerted_disk:
                await self._alert(
                    f"⚠️ Espace disque faible: {free_gb} GB restants, {CFG.OWNER}.", "warn"
                )
                alerted_disk = True
            elif free_gb >= 15:
                alerted_disk = False
            await asyncio.sleep(300)

    # ─── Publication d'alertes sur Redis ─────────────────────────────────
    async def _alert(self, message: str, level: str = "info"):
        await self._redis.publish(CFG.CH_HB, json.dumps({
            "type":    "heartbeat_alert",
            "level":   level,
            "message": message,
            "ts":      time.time(),
        }))
        print(f"[Heartbeat] [{level.upper()}] {message}")
