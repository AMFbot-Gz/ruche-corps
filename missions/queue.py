"""
missions/queue.py — File de missions persistante (Redis + JSONL backup)

Permet de soumettre des missions complexes qui seront exécutées
une par une, même si l'agent tourne toute la nuit.
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis

from config import CFG

QUEUE_KEY  = "ruche:missions:queue"    # Redis LIST — FIFO
ACTIVE_KEY = "ruche:missions:active"   # Redis STRING — mission en cours
DONE_KEY   = "ruche:missions:done"     # Redis LIST — missions terminées
BACKUP_FILE = Path.home() / ".ruche" / "missions_backup.jsonl"


class MissionQueue:
    """File de missions thread-safe basée sur Redis."""

    def __init__(self, redis_client):
        self._redis = redis_client

    async def push(self, mission: str, priority: int = 3, source: str = "cli") -> str:
        """Ajoute une mission à la file. Retourne son ID."""
        mid = f"m_{int(time.time()*1000)}"
        payload = json.dumps({
            "id":         mid,
            "mission":    mission,
            "priority":   priority,
            "source":     source,
            "created_at": datetime.now().isoformat(),
            "status":     "queued",
        }, ensure_ascii=False)
        await self._redis.rpush(QUEUE_KEY, payload)
        # Backup fichier (non-critique : l'échec ne bloque pas la mission)
        try:
            BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(BACKUP_FILE, "a") as f:
                f.write(payload + "\n")
        except Exception:
            pass
        return mid

    async def pop(self) -> Optional[dict]:
        """Retire la prochaine mission de la file."""
        raw = await self._redis.lpop(QUEUE_KEY)
        if raw:
            return json.loads(raw)
        return None

    async def peek(self) -> Optional[dict]:
        """Regarde la prochaine sans la retirer."""
        raw = await self._redis.lindex(QUEUE_KEY, 0)
        if raw:
            return json.loads(raw)
        return None

    async def size(self) -> int:
        return await self._redis.llen(QUEUE_KEY)

    async def list_pending(self) -> list:
        raws = await self._redis.lrange(QUEUE_KEY, 0, -1)
        return [json.loads(r) for r in raws]

    async def set_active(self, mission: dict):
        await self._redis.set(ACTIVE_KEY, json.dumps(mission, ensure_ascii=False))

    async def get_active(self) -> Optional[dict]:
        raw = await self._redis.get(ACTIVE_KEY)
        return json.loads(raw) if raw else None

    async def mark_done(self, mission: dict, result: str):
        mission["status"]      = "done"
        mission["done_at"]     = datetime.now().isoformat()
        mission["result_summary"] = result[:200]
        await self._redis.delete(ACTIVE_KEY)
        await self._redis.lpush(DONE_KEY, json.dumps(mission, ensure_ascii=False))
        await self._redis.ltrim(DONE_KEY, 0, 49)  # garder les 50 dernières

    async def clear(self):
        await self._redis.delete(QUEUE_KEY, ACTIVE_KEY)

    async def status(self) -> dict:
        pending = await self.size()
        active  = await self.get_active()
        raws    = await self._redis.lrange(DONE_KEY, 0, 4)
        done    = [json.loads(r) for r in raws]
        return {
            "pending": pending,
            "active":  active,
            "recent_done": done,
        }
