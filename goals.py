"""
goals.py — Boucle d'objectifs autonomes de La Ruche

L'agent génère ses propres objectifs, les priorise, les exécute
via le worker de missions, et apprend de ses résultats.

Boucle: toutes les 30min → check objectifs → exécuter le plus urgent → log résultat
"""
import asyncio
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import httpx
import psutil
import redis.asyncio as aioredis

from config import CFG
from core.logger import get_logger

log = get_logger(__name__)

GOALS_DB = Path.home() / ".ruche" / "goals.db"
GOALS_DB.parent.mkdir(parents=True, exist_ok=True)

# Intervalle entre chaque cycle d'exécution d'objectif (30 min)
LOOP_INTERVAL_SEC = 30 * 60
# Intervalle entre chaque génération automatique d'objectifs (6h)
GENERATE_INTERVAL_SEC = 6 * 60 * 60


class GoalStatus(Enum):
    PENDING  = "pending"
    ACTIVE   = "active"
    DONE     = "done"
    FAILED   = "failed"
    DEFERRED = "deferred"


class Goal:
    """Représente un objectif autonome."""

    def __init__(
        self,
        id: str,
        description: str,
        priority: int,
        category: str,
        status: str = GoalStatus.PENDING.value,
        created_at: str = None,
        executed_at: str = None,
        result: str = None,
        error: str = None,
        mission_id: str = None,
        learned: str = None,
    ):
        self.id          = id
        self.description = description
        self.priority    = priority
        self.category    = category
        self.status      = status
        self.created_at  = created_at or datetime.now().isoformat()
        self.executed_at = executed_at
        self.result      = result
        self.error       = error
        self.mission_id  = mission_id
        self.learned     = learned

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "description": self.description,
            "priority":    self.priority,
            "category":    self.category,
            "status":      self.status,
            "created_at":  self.created_at,
            "executed_at": self.executed_at,
            "result":      self.result,
            "error":       self.error,
            "mission_id":  self.mission_id,
            "learned":     self.learned,
        }

    @classmethod
    def from_row(cls, row: tuple) -> "Goal":
        return cls(
            id=row[0],
            description=row[1],
            priority=row[2],
            category=row[3],
            status=row[4],
            created_at=row[5],
            executed_at=row[6],
            result=row[7],
            error=row[8],
            mission_id=row[9],
            learned=row[10],
        )


class GoalsLoop:
    """
    Génère et exécute des objectifs autonomes.

    Méthodes principales:
    - async run(): boucle principale (toutes les 30min)
    - async generate_goals(): demande à Nemotron de proposer 3 nouveaux objectifs
    - async pick_next() → Goal | None: sélectionne le prochain objectif
    - async execute(goal) → str: soumet comme mission au worker queue
    - async learn(goal, result): met à jour la base + génère insights
    - add_goal(description, priority, category): ajouter un objectif depuis l'extérieur
    - list_goals() → list[Goal]: lister tous les objectifs actifs/pending
    - get_stats() → dict: stats (done/failed/pending/success_rate)
    """

    def __init__(self, redis_client=None):
        self._redis           = redis_client
        self._db_path         = GOALS_DB
        self._last_generated  = 0.0
        self._init_db()

    # ─── Initialisation SQLite ────────────────────────────────────────────────

    def _init_db(self):
        """Crée la table goals si elle n'existe pas encore."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goals (
                    id          TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    priority    INTEGER NOT NULL DEFAULT 5,
                    category    TEXT NOT NULL DEFAULT 'general',
                    status      TEXT NOT NULL DEFAULT 'pending',
                    created_at  TEXT,
                    executed_at TEXT,
                    result      TEXT,
                    error       TEXT,
                    mission_id  TEXT,
                    learned     TEXT
                )
            """)
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    # ─── CRUD objectifs ───────────────────────────────────────────────────────

    def add_goal(
        self,
        description: str,
        priority: int = 5,
        category: str = "general",
    ) -> str:
        """Ajoute un objectif dans la base. Retourne son ID."""
        gid = f"g_{uuid.uuid4().hex[:8]}"
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO goals (id, description, priority, category, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (gid, description, priority, category,
                 GoalStatus.PENDING.value, datetime.now().isoformat()),
            )
            conn.commit()
        print(f"[Goals] Objectif ajouté : [{gid}] {description[:60]}")
        return gid

    def _update_goal(self, goal: Goal):
        """Sauvegarde l'état complet d'un objectif."""
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE goals SET
                    description=?, priority=?, category=?, status=?,
                    executed_at=?, result=?, error=?, mission_id=?, learned=?
                   WHERE id=?""",
                (goal.description, goal.priority, goal.category, goal.status,
                 goal.executed_at, goal.result, goal.error,
                 goal.mission_id, goal.learned, goal.id),
            )
            conn.commit()

    def list_goals(self) -> list["Goal"]:
        """Retourne tous les objectifs actifs ou en attente."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM goals WHERE status IN ('pending','active') ORDER BY priority DESC"
            ).fetchall()
        return [Goal.from_row(r) for r in rows]

    def get_stats(self) -> dict[str, Any]:
        """Retourne les statistiques d'exécution."""
        with self._get_conn() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
            done    = conn.execute("SELECT COUNT(*) FROM goals WHERE status='done'").fetchone()[0]
            failed  = conn.execute("SELECT COUNT(*) FROM goals WHERE status='failed'").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM goals WHERE status='pending'").fetchone()[0]
            active  = conn.execute("SELECT COUNT(*) FROM goals WHERE status='active'").fetchone()[0]
        success_rate = round(done / (done + failed) * 100) if (done + failed) > 0 else 0
        return {
            "total":        total,
            "done":         done,
            "failed":       failed,
            "pending":      pending,
            "active":       active,
            "success_rate": success_rate,
        }

    # ─── Logique principale ───────────────────────────────────────────────────

    async def pick_next(self) -> Optional[Goal]:
        """Sélectionne le prochain objectif à exécuter (priorité la plus haute)."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM goals WHERE status='pending' ORDER BY priority DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return Goal.from_row(row)

    async def generate_goals(self):
        """Demande à Nemotron de proposer 3 nouveaux objectifs basés sur l'état du système."""
        # Recueillir l'état système
        try:
            disk = psutil.disk_usage("/")
            mem  = psutil.virtual_memory()
            disk_pct = disk.percent
            mem_pct  = mem.percent
        except Exception:
            disk_pct = mem_pct = 0.0

        # Récupérer les erreurs récentes depuis les logs
        recent_errors = self._get_recent_errors()

        prompt = (
            f"En tant qu'agent IA autonome sur macOS, "
            f"basé sur l'état système actuel [disk={disk_pct}%, mem={mem_pct}%, "
            f"recent_errors={recent_errors}], "
            f"propose 3 objectifs utiles et réalisables pour améliorer le système. "
            f'Format JSON uniquement (sans markdown) : '
            f'[{{"description": "...", "priority": 1-10, "category": "maintenance|monitoring|optimization|learning|reporting"}}]'
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                resp = await c.post(
                    f"{CFG.OLLAMA}/api/chat",
                    json={
                        "model":  CFG.M_GENERAL,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {
                            "temperature": CFG.NEMOTRON_TEMP,
                            "num_predict": 600,
                            "num_ctx":     CFG.NEMOTRON_CTX,
                        },
                    },
                )
            raw  = resp.json().get("message", {}).get("content", "[]")
            # Extraire le JSON du texte (peut contenir du markdown)
            m    = re.search(r'\[[\s\S]*\]', raw)
            data = json.loads(m.group()) if m else []
        except Exception as e:
            print(f"[Goals] Erreur génération: {e}")
            data = []

        # Catégories valides
        valid_cats = {"maintenance", "monitoring", "optimization", "learning", "reporting"}
        added = 0
        for item in data[:3]:
            if not isinstance(item, dict) or not item.get("description"):
                continue
            cat = item.get("category", "general")
            if cat not in valid_cats:
                log.warning("goal_category_remapped", original=cat, remapped="general")
                cat = "general"
            self.add_goal(
                description=item["description"],
                priority=max(1, min(10, int(item.get("priority", 5)))),
                category=cat,
            )
            added += 1

        print(f"[Goals] {added} nouveaux objectifs générés automatiquement")
        self._last_generated = time.time()
        return added

    def _get_recent_errors(self) -> str:
        """Lit les dernières lignes du log worker pour trouver des erreurs récentes."""
        log_path = Path.home() / ".ruche" / "logs" / "worker.log"
        if not log_path.exists():
            return "aucune"
        try:
            lines = log_path.read_text(errors="replace").splitlines()
            errors = [l for l in lines[-200:] if "ERREUR" in l or "ERROR" in l or "error" in l.lower()]
            return "; ".join(errors[-3:]) if errors else "aucune"
        except Exception:
            return "aucune"

    async def execute(self, goal: Goal) -> str:
        """Soumet l'objectif comme mission dans la file Redis du worker."""
        goal.status      = GoalStatus.ACTIVE.value
        goal.executed_at = datetime.now().isoformat()
        self._update_goal(goal)

        if self._redis:
            try:
                # Générer un ID de mission unique
                mission_id = f"m_{int(time.time()*1000)}"
                payload = json.dumps({
                    "id":         mission_id,
                    "mission":    goal.description,
                    "priority":   goal.priority,
                    "source":     "goals_loop",
                    "created_at": datetime.now().isoformat(),
                    "status":     "queued",
                }, ensure_ascii=False)
                await self._redis.rpush("ruche:missions:queue", payload)
                goal.mission_id = mission_id
                self._update_goal(goal)
                return f"Mission soumise: {mission_id}"
            except Exception as e:
                return f"ERREUR soumission Redis: {e}"
        else:
            return "ERREUR: Redis non disponible"

    async def learn(self, goal: Goal, result: str):
        """Met à jour la base après exécution + génère un insight."""
        success = not result.startswith("ERREUR")
        goal.status = GoalStatus.DONE.value if success else GoalStatus.FAILED.value
        goal.result = result[:500]

        # Générer un insight court
        insight = await self._generate_insight(goal, result)
        goal.learned = insight
        self._update_goal(goal)

        stats = self.get_stats()
        rate = stats['success_rate']
        rate_str = f"{rate}%" if (stats['done'] + stats['failed']) > 0 else "N/A (aucun terminé)"
        print(
            f"[Goals] Objectif {'terminé' if success else 'échoué'}: {goal.id} "
            f"| succès global: {rate_str}"
        )

    async def _generate_insight(self, goal: Goal, result: str) -> str:
        """Demande à Nemotron un insight court sur le résultat."""
        prompt = (
            f"Objectif '{goal.description}' résultat: '{result[:200]}'. "
            f"En 1 phrase: qu'est-ce qu'on apprend de ceci pour l'avenir?"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                resp = await c.post(
                    f"{CFG.OLLAMA}/api/chat",
                    json={
                        "model":  CFG.M_GENERAL,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 100},
                    },
                )
            return resp.json().get("message", {}).get("content", "").strip()[:300]
        except Exception:
            return ""

    @staticmethod
    async def _learn_from_mission(mission_data: dict, plan: dict):
        """
        Point d'entrée appelé depuis worker.py après chaque exécution de mission.
        Met à jour l'objectif lié à cette mission s'il existe.
        """
        mission_id = mission_data.get("id", "")
        if not mission_id:
            return

        db_path = GOALS_DB
        if not db_path.exists():
            return

        try:
            conn = sqlite3.connect(str(db_path))
            row  = conn.execute(
                "SELECT * FROM goals WHERE mission_id=?", (mission_id,)
            ).fetchone()
            conn.close()
        except Exception:
            return

        if not row:
            return

        goal     = Goal.from_row(row)
        progress = plan.get("progress", 0)
        errors   = plan.get("errors", 0)
        result   = f"{progress}% complet, {errors} erreurs"

        goal.status = GoalStatus.DONE.value if errors == 0 else GoalStatus.FAILED.value
        goal.result = result
        goal.learned = f"Mission {progress}% complète avec {errors} erreur(s)."

        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """UPDATE goals SET status=?, result=?, learned=? WHERE id=?""",
                (goal.status, goal.result, goal.learned, goal.id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Goals] Erreur mise à jour post-mission: {e}")

    # ─── Boucle principale ────────────────────────────────────────────────────

    async def run(self):
        """Boucle principale : toutes les 30 min, exécute le prochain objectif prioritaire."""
        print("[Goals] Boucle d'objectifs autonomes démarrée")
        # Générer des objectifs au démarrage
        await self.generate_goals()

        # Déclencher réflexion si c'est le premier cycle après 3h00
        from core.metacognition import get_metacognition
        meta = get_metacognition()
        asyncio.create_task(meta.schedule())  # Lance en background

        while True:
            try:
                # Génération automatique toutes les 6h
                if time.time() - self._last_generated >= GENERATE_INTERVAL_SEC:
                    await self.generate_goals()

                # Choisir le prochain objectif
                goal = await self.pick_next()
                if goal:
                    print(f"[Goals] Exécution : [{goal.id}] {goal.description[:70]}")
                    result = await self.execute(goal)
                    await self.learn(goal, result)
                else:
                    print("[Goals] Aucun objectif en attente — attente...")

                stats = self.get_stats()
                rate = stats['success_rate']
                rate_str = f"{rate}%" if (stats['done'] + stats['failed']) > 0 else "N/A (aucun terminé)"
                print(
                    f"[Goals] Stats : {stats['done']} terminés / "
                    f"{stats['pending']} en attente / "
                    f"taux succès {rate_str}"
                )

            except Exception as e:
                print(f"[Goals] Erreur boucle: {e}")

            await asyncio.sleep(LOOP_INTERVAL_SEC)


# ─── Point d'entrée standalone ────────────────────────────────────────────────

async def _main():
    redis = None
    try:
        redis = await aioredis.from_url(CFG.REDIS)
        print("[Goals] Connecté à Redis")
    except Exception as e:
        print(f"[Goals] Redis non disponible ({e}) — mode dégradé")

    loop = GoalsLoop(redis_client=redis)
    try:
        await loop.run()
    finally:
        if redis:
            await redis.aclose()


if __name__ == "__main__":
    import signal

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    def _stop(*_):
        print("\n[Goals] Arrêt demandé.")
        event_loop.stop()

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        event_loop.run_until_complete(_main())
    except (asyncio.CancelledError, RuntimeError):
        pass
    finally:
        event_loop.close()
        print("[Goals] Arrêté proprement.")
