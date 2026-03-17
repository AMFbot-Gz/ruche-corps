"""
worker.py — Service autonome de La Ruche (Mode Nuit)

Tourne en arrière-plan, consomme la file de missions,
planifie et exécute chaque mission tâche par tâche,
même sans surveillance.

Usage :
    python3 worker.py              # démarre le worker
    python3 worker.py --status     # état de la file
    python3 worker.py --add "..."  # ajouter une mission
    python3 worker.py --clear      # vider la file
"""
import argparse
import asyncio
import json
import signal
import sys
import time

import redis.asyncio as aioredis

from config import CFG
from missions.queue import MissionQueue
from missions.planner import decompose
from missions.executor import MissionExecutor
from tools.registry import registry
import tools.builtins  # enregistrement des outils disponibles

# Import optionnel de goals — non bloquant si absent
try:
    from goals import GoalsLoop as _GoalsLoop
    _GOALS_AVAILABLE = True
except ImportError:
    _GOALS_AVAILABLE = False


async def worker_loop():
    """
    Boucle principale du worker.
    Attend des missions, les planifie, les exécute, recommence.
    """
    redis  = await aioredis.from_url(CFG.REDIS)
    queue  = MissionQueue(redis)
    exec_  = MissionExecutor(redis)

    print(f"[Worker] 🦾 Démarré — {len(registry.list_tools())} outils")
    print(f"[Worker] 📋 Modèle : {CFG.M_GENERAL}")
    print(f"[Worker] ⏳ En attente de missions sur la file Redis...")

    # Reprendre une mission active (crash recovery)
    active = await queue.get_active()
    if active:
        print(f"[Worker] ↩️  Reprise mission interrompue : {active['mission'][:60]}")
        await _run_mission(active, queue, exec_)

    # Écoute aussi les messages inbound pour les missions en temps réel
    async def inbound_listener():
        async with redis.pubsub() as ps:
            await ps.subscribe(CFG.CH_IN)
            async for msg in ps.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data    = json.loads(msg["data"])
                    text    = data.get("text", "").strip()
                    channel = data.get("channel", "")
                    # Détecte les missions longues (mots-clés)
                    if _is_mission(text):
                        mid = await queue.push(text, source=channel)
                        print(f"[Worker] Mission ajoutée automatiquement: {mid}")
                except Exception:
                    pass
    asyncio.create_task(inbound_listener())

    # Boucle principale
    while True:
        mission_data = await queue.pop()
        if mission_data:
            await _run_mission(mission_data, queue, exec_)
        else:
            await asyncio.sleep(3)  # attendre de nouvelles missions

    await redis.aclose()


def _is_mission(text: str) -> bool:
    """Détecte si un message ressemble à une mission longue (pas une question courte)."""
    if len(text) < 30:
        return False
    keywords = [
        "toute la nuit", "en arrière-plan", "tâche par tâche",
        "mission:", "travail:", "projet:", "analyse complète",
        "refactor", "migrer", "construire", "créer un", "programmer",
        "déployer", "tester tous", "audit complet",
    ]
    return any(kw in text.lower() for kw in keywords)


async def _run_mission(mission_data: dict, queue: MissionQueue, exec_: MissionExecutor):
    """Planifie et exécute une mission complète."""
    mission = mission_data["mission"]
    print(f"\n[Worker] ═══ Mission : {mission[:80]} ═══")

    await queue.set_active(mission_data)

    # 1. Planification HTN via Nemotron
    print(f"[Worker] 🧠 Planification en cours...")
    tools_list = registry.list_tools()
    plan = await decompose(mission, tools=tools_list)
    print(f"[Worker] 📋 Plan : {len(plan['tasks'])} tâches, complexité={plan['complexity']}")
    for t in plan["tasks"]:
        print(f"  [{t['id']}] {t['description'][:70]}")

    # 2. Exécution tâche par tâche
    plan = await exec_.run(plan, report_every=3)

    # 2b. Apprentissage goals (si goals.py est disponible)
    if _GOALS_AVAILABLE:
        try:
            await _GoalsLoop._learn_from_mission(mission_data, plan)
        except Exception as _ge:
            print(f"[Worker] goals.learn_from_mission ignoré: {_ge}")

    # 3. Marquer comme terminée
    result_summary = f"{plan['progress']}% complet, {plan.get('errors',0)} erreurs"
    await queue.mark_done(mission_data, result_summary)

    print(f"[Worker] ✅ Mission terminée : {result_summary}")


# ─── Commandes CLI ────────────────────────────────────────────

async def cmd_status():
    redis = await aioredis.from_url(CFG.REDIS)
    queue = MissionQueue(redis)
    s     = await queue.status()
    print(f"\n📋 File de missions:")
    print(f"  En attente : {s['pending']}")
    print(f"  Active     : {s['active']['mission'][:60] if s['active'] else 'aucune'}")
    print(f"\n✅ Dernières terminées :")
    for m in s['recent_done'][:5]:
        print(f"  • {m['mission'][:60]} — {m.get('result_summary','')}")
    await redis.aclose()


async def cmd_add(mission: str, priority: int = 3):
    redis = await aioredis.from_url(CFG.REDIS)
    queue = MissionQueue(redis)
    mid   = await queue.push(mission, priority=priority)
    size  = await queue.size()
    print(f"✅ Mission ajoutée : {mid} (position {size} dans la file)")
    await redis.aclose()


async def cmd_clear():
    redis = await aioredis.from_url(CFG.REDIS)
    queue = MissionQueue(redis)
    await queue.clear()
    print("File vidée.")
    await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="La Ruche — Worker de missions autonome")
    parser.add_argument("--status",   action="store_true", help="Afficher l'état de la file")
    parser.add_argument("--add",      type=str,            help="Ajouter une mission")
    parser.add_argument("--priority", type=int, default=3, help="Priorité de la mission (1-5)")
    parser.add_argument("--clear",    action="store_true", help="Vider la file de missions")
    args = parser.parse_args()

    if args.status:
        asyncio.run(cmd_status())
    elif args.add:
        asyncio.run(cmd_add(args.add, args.priority))
    elif args.clear:
        asyncio.run(cmd_clear())
    else:
        # Mode worker : tourne jusqu'à Ctrl+C
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def _stop(*_):
            print("\n[Worker] Arrêt demandé.")
            loop.stop()
        signal.signal(signal.SIGINT,  _stop)
        signal.signal(signal.SIGTERM, _stop)

        try:
            loop.run_until_complete(worker_loop())
        except (asyncio.CancelledError, RuntimeError):
            pass
        finally:
            loop.close()
            print("[Worker] Arrêté proprement.")
