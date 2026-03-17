"""
heartbeat.py — Système autonome de La Ruche

Tourne en arrière-plan, surveille, anticipe, agit sans qu'on lui demande.
Comme le système nerveux autonome : le cœur bat sans que le cerveau l'ordonne.
"""
import asyncio
import json
import time
from datetime import datetime, date

import httpx
import redis.asyncio as aioredis

from config import CFG
from core.logger import get_logger

log = get_logger(__name__)


class HeartbeatService:
    def __init__(self):
        # Pas de client httpx persistant — on utilise des context managers dans chaque méthode
        self._redis = None
        self._down  = {}
        # Clé = date du jour (date object), valeur = bool
        # Permet un reset automatique à minuit sans logique explicite
        self._briefing_done: dict[date, bool] = {}

    async def start(self, redis_client=None):
        self._redis = redis_client or await aioredis.from_url(CFG.REDIS)
        log.info("heartbeat_started")
        await asyncio.gather(
            self._health_loop(),
            self._briefing_loop(),
            self._disk_monitor(),
        )

    # ─── Containers Docker critiques à surveiller ─────────────────────────
    DOCKER_SERVICES = {
        "revenue-os-postgres": "Postgres",
        "revenue-os-redis":    "Redis",
        "n8n-openclaw":        "N8N",
    }

    # ─── Boucle de santé (60s) ────────────────────────────────────────────
    async def _health_loop(self):
        checks = {
            "Ollama":    f"{CFG.OLLAMA}/api/tags",
            "Ghost OS":  f"{CFG.GHOST_URL}/api/health",
            "Comp. Use": f"{CFG.GHOST_CU}/health",
        }
        while True:
            # Vérification des services HTTP
            async with httpx.AsyncClient(timeout=5.0) as c:
                for name, url in checks.items():
                    try:
                        await c.get(url)
                        if self._down.get(name):
                            await self._alert(f"{name} est de retour en ligne, {CFG.OWNER}.", "info")
                            self._down[name] = False
                            log.info("service_recovered", service=name)
                    except Exception as e:
                        if not self._down.get(name):
                            await self._alert(f"⚠️ {name} est tombé, {CFG.OWNER}.", "warn")
                            self._down[name] = True
                            log.warning("service_down", service=name, error=str(e))

            # Vérification des containers Docker critiques
            await self._check_docker_services()

            await asyncio.sleep(60)

    async def _check_docker_services(self):
        """
        Vérifie que les containers Docker critiques sont en état 'running'.
        Utilise `docker inspect --format='{{.State.Status}}' <name>` pour chaque container.
        """
        for container, label in self.DOCKER_SERVICES.items():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "inspect",
                    "--format", "{{.State.Status}}",
                    container,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                status = stdout.decode().strip()

                service_key = f"docker:{container}"
                if status == "running":
                    # Container de retour après une panne
                    if self._down.get(service_key):
                        await self._alert(
                            f"Docker {label} ({container}) est de retour en ligne, {CFG.OWNER}.",
                            "info",
                        )
                        self._down[service_key] = False
                        log.info("docker_recovered", container=container)
                else:
                    # Container absent (not found) ou dans un état anormal
                    display_status = status or "introuvable"
                    if not self._down.get(service_key):
                        await self._alert(
                            f"⚠️ Docker {label} ({container}) : état '{display_status}', {CFG.OWNER}.",
                            "warn",
                        )
                        self._down[service_key] = True
                        log.warning("docker_not_running",
                                    container=container, status=display_status)
            except asyncio.TimeoutError:
                log.warning("docker_check_timeout", container=container)
            except FileNotFoundError:
                # Docker non installé — on ne surveille pas dans ce cas
                log.debug("docker_not_installed")
                break
            except Exception as e:
                log.warning("docker_check_error", container=container, error=str(e))

    # ─── Briefing matinal (8h00) ──────────────────────────────────────────
    async def _briefing_loop(self):
        while True:
            now      = datetime.now()
            today    = now.date()
            # Le briefing n'est fait qu'une seule fois par date calendaire
            already  = self._briefing_done.get(today, False)
            if now.hour == 8 and now.minute < 5 and not already:
                await self._morning_briefing()
                self._briefing_done[today] = True
                # Nettoyage des entrées passées pour éviter la croissance infinie
                self._briefing_done = {k: v for k, v in self._briefing_done.items() if k >= today}
            await asyncio.sleep(60)

    async def _morning_briefing(self):
        status_parts = []

        async with httpx.AsyncClient(timeout=5.0) as c:
            try:
                r        = await c.get(f"{CFG.GHOST_URL}/api/status")
                data     = r.json()
                missions = data.get("missions", {}).get("total", 0)
                uptime   = data.get("uptime", 0)
                status_parts.append(f"Ghost OS: actif depuis {uptime // 3600}h, {missions} missions totales")
            except Exception as e:
                status_parts.append("Ghost OS: indisponible")
                log.warning("briefing_ghost_unavailable", error=str(e))

            try:
                r      = await c.get(f"{CFG.OLLAMA}/api/tags")
                models = r.json().get("models", [])
                status_parts.append(f"Ollama: {len(models)} modèles disponibles")
            except Exception as e:
                status_parts.append("Ollama: indisponible")
                log.warning("briefing_ollama_unavailable", error=str(e))

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
        except Exception as e:
            briefing = f"Bonjour {CFG.OWNER}, systèmes opérationnels. Bonne journée."
            log.warning("briefing_llm_error", error=str(e))

        await self._alert(briefing, "briefing")
        log.info("briefing_sent", preview=briefing[:80])

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
                log.warning("disk_low", free_gb=free_gb)
            elif free_gb >= 15:
                alerted_disk = False
            await asyncio.sleep(300)

    # ─── Publication d'alertes sur Redis ─────────────────────────────────
    async def _alert(self, message: str, level: str = "info"):
        try:
            await self._redis.publish(CFG.CH_HB, json.dumps({
                "type":    "heartbeat_alert",
                "level":   level,
                "message": message,
                "ts":      time.time(),
            }))
            log.info("alert_published", level=level, message=message[:80])
        except Exception as e:
            print(f"[Heartbeat] ALERT REDIS FAILED: {e} | msg: {message[:100]}")
