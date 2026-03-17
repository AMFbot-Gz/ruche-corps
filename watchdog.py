"""
watchdog.py — Surveillance et auto-réparation de La Ruche

Surveille: agent principal, worker, heartbeat
Détecte: process mort, Redis injoignable, Ollama down, mémoire trop haute
Répare: relance les services morts, notifie Telegram

Usage: python3 watchdog.py (daemon)
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil
import redis.asyncio as aioredis

from config import CFG

# ─── Configuration des services surveillés ────────────────────────────────────
DIR = Path(__file__).parent.resolve()

SERVICES = {
    "worker": {
        "cmd":          ["python3", "-u", str(DIR / "worker.py")],
        "pid_file":     str(DIR / ".worker.pid"),
        "log":          str(Path.home() / ".ruche" / "logs" / "worker.log"),
        "max_restarts": 5,
        "restart_delay": 10,
    },
    # agent et heartbeat surveillés mais non relancés — c'est main.py qui les gère
}

# Intervalle de vérification (30s)
CHECK_INTERVAL_SEC = 30


class Watchdog:
    """
    Surveille les services de La Ruche et les relance en cas de panne.
    """

    def __init__(self, redis_client=None):
        self._redis    = redis_client
        # Compteurs de redémarrages par service
        self._restarts: dict[str, int] = {svc: 0 for svc in SERVICES}
        # Timestamp du dernier redémarrage par service
        self._last_restart: dict[str, float] = {svc: 0.0 for svc in SERVICES}

    # ─── Vérifications ────────────────────────────────────────────────────────

    def is_alive(self, service_name: str) -> bool:
        """Vérifie si un service est vivant via son PID file + kill -0."""
        cfg      = SERVICES.get(service_name)
        if not cfg:
            return False
        pid_file = Path(cfg["pid_file"])
        if not pid_file.exists():
            return False
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)   # kill -0 : vérifie l'existence sans envoyer de signal
            return True
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            return False

    async def check_redis(self) -> bool:
        """Ping Redis."""
        if not self._redis:
            return False
        try:
            return await self._redis.ping()
        except Exception:
            return False

    async def check_ollama(self) -> bool:
        """GET /api/tags sur Ollama."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{CFG.OLLAMA}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    def check_memory(self) -> bool:
        """Vérifie que la RAM est en dessous de 85%."""
        try:
            return psutil.virtual_memory().percent < 85.0
        except Exception:
            return True

    def check_disk(self) -> bool:
        """Vérifie que le disque est en dessous de 90%."""
        try:
            return psutil.disk_usage("/").percent < 90.0
        except Exception:
            return True

    # ─── Redémarrage ─────────────────────────────────────────────────────────

    async def restart(self, service_name: str):
        """Relance un service mort, met à jour le PID file, notifie Telegram."""
        cfg = SERVICES.get(service_name)
        if not cfg:
            await self._alert(f"[Watchdog] Service inconnu : {service_name}")
            return

        count = self._restarts[service_name]
        if count >= cfg["max_restarts"]:
            msg = (
                f"[Watchdog] ALERTE : {service_name} est mort et a dépassé "
                f"la limite de {cfg['max_restarts']} redémarrages. "
                f"Intervention manuelle requise."
            )
            await self._alert(msg)
            print(f"[Watchdog] {msg}")
            return

        # Respecter le délai entre les redémarrages
        elapsed = time.time() - self._last_restart[service_name]
        if elapsed < cfg["restart_delay"]:
            await asyncio.sleep(cfg["restart_delay"] - elapsed)

        print(f"[Watchdog] Redémarrage {service_name} (tentative {count + 1}/{cfg['max_restarts']})...")

        # S'assurer que le répertoire de logs existe
        log_path = Path(cfg["log"]).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(log_path, "a") as log_file:
                proc = subprocess.Popen(
                    cfg["cmd"],
                    stdout=log_file,
                    stderr=log_file,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    cwd=str(DIR),
                )

            # Enregistrer le nouveau PID
            pid_file = Path(cfg["pid_file"])
            pid_file.write_text(str(proc.pid))

            self._restarts[service_name]    += 1
            self._last_restart[service_name] = time.time()

            msg = (
                f"[Watchdog] {service_name} relancé (PID {proc.pid}, "
                f"tentative {self._restarts[service_name]}/{cfg['max_restarts']})"
            )
            print(msg)
            await self._alert(msg)

        except Exception as e:
            msg = f"[Watchdog] Échec relancement {service_name}: {e}"
            print(msg)
            await self._alert(msg)

    # ─── Vérification globale ─────────────────────────────────────────────────

    async def check_all(self):
        """Vérifie tous les services et ressources. Relance si nécessaire."""
        # Services avec PID file
        for svc_name in SERVICES:
            if not self.is_alive(svc_name):
                print(f"[Watchdog] {svc_name} : mort détecté")
                await self._alert(f"[Watchdog] {svc_name} est mort — tentative de relancement...")
                await self.restart(svc_name)
            else:
                print(f"[Watchdog] {svc_name} : OK (PID {self._get_pid(svc_name)})")

        # Redis
        redis_ok = await self.check_redis()
        if not redis_ok:
            await self._alert("[Watchdog] ALERTE : Redis injoignable !")
            print("[Watchdog] Redis : MORT")
        else:
            print("[Watchdog] Redis : OK")

        # Ollama
        ollama_ok = await self.check_ollama()
        if not ollama_ok:
            await self._alert("[Watchdog] ALERTE : Ollama injoignable !")
            print("[Watchdog] Ollama : MORT")
        else:
            print("[Watchdog] Ollama : OK")

        # Mémoire
        if not self.check_memory():
            mem_pct = psutil.virtual_memory().percent
            await self._alert(f"[Watchdog] ALERTE : RAM à {mem_pct:.1f}% (seuil 85%)")
            print(f"[Watchdog] Mémoire : CRITIQUE ({mem_pct:.1f}%)")
        else:
            print(f"[Watchdog] Mémoire : OK ({psutil.virtual_memory().percent:.1f}%)")

        # Disque
        if not self.check_disk():
            disk_pct = psutil.disk_usage("/").percent
            await self._alert(f"[Watchdog] ALERTE : Disque à {disk_pct:.1f}% (seuil 90%)")
            print(f"[Watchdog] Disque : CRITIQUE ({disk_pct:.1f}%)")
        else:
            print(f"[Watchdog] Disque : OK ({psutil.disk_usage('/').percent:.1f}%)")

    def _get_pid(self, service_name: str) -> int:
        """Lit le PID depuis le fichier PID."""
        cfg = SERVICES.get(service_name, {})
        try:
            return int(Path(cfg.get("pid_file", "")).read_text().strip())
        except Exception:
            return -1

    # ─── Alertes ─────────────────────────────────────────────────────────────

    async def _alert(self, message: str):
        """Publie une alerte sur Redis (→ Telegram via heartbeat)."""
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {message}"
        print(full)
        if self._redis:
            try:
                await self._redis.publish(CFG.CH_HB, json.dumps({
                    "type":    "watchdog_alert",
                    "level":   "warning",
                    "message": full,
                }))
            except Exception:
                pass

    # ─── Boucle principale ────────────────────────────────────────────────────

    async def run(self):
        """Boucle principale du watchdog (toutes les 30s)."""
        print(f"[Watchdog] Démarré — surveillance toutes les {CHECK_INTERVAL_SEC}s")

        # Vérification initiale au démarrage
        await self.check_all()

        while True:
            await asyncio.sleep(CHECK_INTERVAL_SEC)
            try:
                await self.check_all()
            except Exception as e:
                print(f"[Watchdog] Erreur inattendue: {e}")


# ─── Point d'entrée standalone ────────────────────────────────────────────────

async def _main():
    # Écrire notre propre PID
    pid_file = DIR / ".watchdog.pid"
    pid_file.write_text(str(os.getpid()))
    print(f"[Watchdog] PID {os.getpid()} enregistré dans {pid_file}")

    redis = None
    try:
        redis = await aioredis.from_url(CFG.REDIS)
        print("[Watchdog] Connecté à Redis")
    except Exception as e:
        print(f"[Watchdog] Redis non disponible ({e}) — alertes Telegram désactivées")

    wd = Watchdog(redis_client=redis)
    try:
        await wd.run()
    finally:
        if redis:
            await redis.aclose()
        # Nettoyer le PID file
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    def _stop(*_):
        print("\n[Watchdog] Arrêt demandé.")
        event_loop.stop()

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        event_loop.run_until_complete(_main())
    except (asyncio.CancelledError, RuntimeError):
        pass
    finally:
        event_loop.close()
        print("[Watchdog] Arrêté proprement.")
