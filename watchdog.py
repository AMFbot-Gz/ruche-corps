"""
watchdog.py — Surveillance et auto-réparation de La Ruche

Surveille: agent principal, worker, heartbeat
Détecte: process mort, Redis injoignable, Ollama down, mémoire trop haute
Répare: relance les services morts, notifie Telegram

Améliorations (depuis ghost-os-ultimate/src/worldmodel/model.py) :
  - WorldState : snapshot système thread-safe avec écriture atomique
  - check_disk_free_gb() : seuil sur l'espace libre (pas seulement %)
  - Seuils configurables via WATCHDOG_MEM_PCT / WATCHDOG_DISK_PCT

Usage: python3 watchdog.py (daemon)
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil
import redis.asyncio as aioredis

from config import CFG

# ─── Configuration des services surveillés ────────────────────────────────────
DIR = Path(__file__).parent.resolve()

# Seuils configurables via env (surchargeables sans toucher au code)
_MEM_THRESHOLD  = float(os.environ.get("WATCHDOG_MEM_PCT",  "85"))
_DISK_THRESHOLD = float(os.environ.get("WATCHDOG_DISK_PCT", "90"))
_DISK_FREE_MIN_GB = float(os.environ.get("WATCHDOG_DISK_FREE_GB", "2.0"))


# ─── WorldState : snapshot système persistant (adapté de ghost-os-ultimate) ──

class WorldState:
    """
    Snapshot thread-safe de l'état courant du système.
    Persiste dans ~/.ruche/world_state.json via écriture atomique.
    Singleton accessible via WorldState.get_instance().
    """

    _instance: "WorldState | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "WorldState":
        with cls._instance_lock:
            if cls._instance is None:
                from config import RUCHE_DIR
                cls._instance = cls(RUCHE_DIR / "world_state.json")
            return cls._instance

    def __init__(self, state_path: Path):
        self._path  = Path(state_path)
        self._lock  = threading.Lock()
        self._state: dict = self._load()

    def _load(self) -> dict:
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8").strip()
                if raw:
                    return json.loads(raw)
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save(self) -> None:
        """Écriture atomique via fichier temporaire (évite la corruption)."""
        self._state["updated_at"] = datetime.now().isoformat()
        payload = json.dumps(self._state, ensure_ascii=False, indent=2)
        try:
            dir_ = self._path.parent
            dir_.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".world_state_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError:
            pass  # Non bloquant

    def update(self, snapshot: dict) -> None:
        """Fusionne un snapshot système dans l'état courant et persiste."""
        with self._lock:
            sys_keys = (
                "cpu_percent", "ram_percent", "ram_used_gb", "ram_total_gb",
                "disk_percent", "disk_used_gb", "disk_free_gb",
            )
            for k in sys_keys:
                if k in snapshot:
                    if "system" not in self._state:
                        self._state["system"] = {}
                    self._state["system"][k] = snapshot[k]
            self._save()

    def get_system(self) -> dict:
        with self._lock:
            return dict(self._state.get("system", {}))

    def is_disk_space_low(self, threshold_gb: float = _DISK_FREE_MIN_GB) -> bool:
        """True si l'espace libre est inférieur à threshold_gb."""
        with self._lock:
            free = self._state.get("system", {}).get("disk_free_gb")
        if free is None:
            return False
        return float(free) < threshold_gb

    def is_cpu_high(self, threshold: float = 80.0) -> bool:
        with self._lock:
            cpu = self._state.get("system", {}).get("cpu_percent")
        return float(cpu) >= threshold if cpu is not None else False

    def is_ram_critical(self, threshold: float = _MEM_THRESHOLD) -> bool:
        with self._lock:
            ram = self._state.get("system", {}).get("ram_percent")
        return float(ram) >= threshold if ram is not None else False

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

# ─── Containers Docker critiques surveillés ────────────────────────────────────
# Clé = nom du container Docker, Valeur = label lisible pour les alertes
DOCKER_SERVICES = {
    "revenue-os-postgres": "Postgres",
    "revenue-os-redis":    "Redis",
    "n8n-openclaw":        "N8N",
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
        """Vérifie que la RAM est en dessous du seuil configurable."""
        try:
            return psutil.virtual_memory().percent < _MEM_THRESHOLD
        except Exception:
            return True

    def check_disk(self) -> bool:
        """Vérifie que le disque est en dessous du seuil configurable."""
        try:
            return psutil.disk_usage("/").percent < _DISK_THRESHOLD
        except Exception:
            return True

    def check_disk_free_gb(self) -> bool:
        """Vérifie que l'espace libre est supérieur à _DISK_FREE_MIN_GB."""
        try:
            free_gb = psutil.disk_usage("/").free / 1e9
            return free_gb >= _DISK_FREE_MIN_GB
        except Exception:
            return True

    def _collect_world_snapshot(self) -> None:
        """
        Met à jour WorldState avec le snapshot système courant.
        Permet aux autres modules d'accéder à l'état système sans psutil.
        """
        try:
            mem  = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            WorldState.get_instance().update({
                "cpu_percent":  psutil.cpu_percent(interval=0.1),
                "ram_percent":  mem.percent,
                "ram_used_gb":  round(mem.used / 1e9, 2),
                "ram_total_gb": round(mem.total / 1e9, 2),
                "disk_percent": disk.percent,
                "disk_used_gb": round(disk.used / 1e9, 2),
                "disk_free_gb": round(disk.free / 1e9, 2),
            })
        except Exception:
            pass

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

    async def check_docker_services(self):
        """
        Vérifie que les containers Docker critiques sont en état 'running'.
        Utilise `docker inspect --format='{{.State.Status}}' <name>`.
        Les containers manquants ou stoppés déclenchent une alerte.
        """
        for container, label in DOCKER_SERVICES.items():
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

                if status == "running":
                    print(f"[Watchdog] Docker {label} ({container}) : OK")
                else:
                    display = status if status else "introuvable"
                    await self._alert(
                        f"[Watchdog] ALERTE Docker : {label} ({container}) est '{display}'"
                    )
                    print(f"[Watchdog] Docker {label} ({container}) : {display.upper()}")

            except asyncio.TimeoutError:
                print(f"[Watchdog] Docker {label} : timeout inspection")
            except FileNotFoundError:
                # Docker non installé — on skip silencieusement
                print("[Watchdog] Docker non installé — surveillance containers désactivée")
                break
            except Exception as e:
                print(f"[Watchdog] Docker {label} : erreur ({e})")

    async def check_all(self):
        """Vérifie tous les services et ressources. Relance si nécessaire."""
        # Mise à jour du WorldState (snapshot système pour les autres modules)
        self._collect_world_snapshot()

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
            await self._alert(f"[Watchdog] ALERTE : RAM à {mem_pct:.1f}% (seuil {_MEM_THRESHOLD}%)")
            print(f"[Watchdog] Mémoire : CRITIQUE ({mem_pct:.1f}%)")
        else:
            print(f"[Watchdog] Mémoire : OK ({psutil.virtual_memory().percent:.1f}%)")

        # Disque (pourcentage)
        if not self.check_disk():
            disk_pct = psutil.disk_usage("/").percent
            await self._alert(f"[Watchdog] ALERTE : Disque à {disk_pct:.1f}% (seuil {_DISK_THRESHOLD}%)")
            print(f"[Watchdog] Disque : CRITIQUE ({disk_pct:.1f}%)")
        else:
            print(f"[Watchdog] Disque : OK ({psutil.disk_usage('/').percent:.1f}%)")

        # Disque (espace libre absolu)
        if not self.check_disk_free_gb():
            free_gb = psutil.disk_usage("/").free / 1e9
            await self._alert(f"[Watchdog] ALERTE : Espace libre disque {free_gb:.1f} GB (seuil {_DISK_FREE_MIN_GB} GB)")
            print(f"[Watchdog] Espace libre : CRITIQUE ({free_gb:.1f} GB)")

        # Containers Docker critiques
        await self.check_docker_services()

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
