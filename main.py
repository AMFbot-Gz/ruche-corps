"""
main.py — Point d'entrée unique de La Ruche

Usage:
  ./start.sh                # tout actif (recommandé)
  python3 main.py --cli     # terminal interactif
  python3 main.py --test    # health check
  python3 main.py --no-voice --no-telegram  # agent seul, sans entrées
"""
import argparse, asyncio, json, signal, sys
import redis.asyncio as aioredis
from config import CFG


async def health_check() -> bool:
    import httpx
    ok = True
    async with httpx.AsyncClient(timeout=3.0) as c:
        for name, url in [
            ("Ollama",    f"{CFG.OLLAMA}/api/tags"),
            ("Redis",     None),
            ("Ghost OS",  f"{CFG.GHOST_URL}/api/health"),
            ("Comp.Use",  f"{CFG.GHOST_CU}/health"),
        ]:
            if url is None:
                try:
                    r = await aioredis.from_url(CFG.REDIS)
                    await r.ping(); await r.aclose()
                    print(f"  ✅ Redis")
                except Exception:
                    print(f"  ❌ Redis — docker compose up redis -d")
                    ok = False
                continue
            try:
                await c.get(url)
                print(f"  ✅ {name}")
            except Exception:
                req = "ollama" in name.lower()
                print(f"  {'❌' if req else '⚠️ '} {name}{'  (requis)' if req else ' (optionnel)'}")
                if req:
                    ok = False
    return ok


async def cli_loop(redis_client):
    print("\n[CLI] Mode terminal — tapez votre message (q pour quitter)\n")

    async def printer():
        async with redis_client.pubsub() as ps:
            await ps.subscribe(CFG.CH_OUT, CFG.CH_STREAM)
            async for msg in ps.listen():
                if msg["type"] != "message": continue
                try:
                    d = json.loads(msg["data"])
                    if msg["channel"] == CFG.CH_STREAM.encode():
                        print(d.get("token",""), end="", flush=True)
                    else:
                        print()  # fin du stream
                except Exception:
                    pass
    asyncio.create_task(printer())

    while True:
        try:
            text = await asyncio.to_thread(input, f"\n{CFG.OWNER}: ")
            if text.strip().lower() in ("q","quit","exit"):
                break
            if not text.strip():
                continue
            await redis_client.publish(CFG.CH_IN, json.dumps({
                "channel": "cli", "user_id": "local",
                "text": text.strip(), "session_id": "cli:local"
            }))
        except (EOFError, KeyboardInterrupt):
            break


async def main():
    parser = argparse.ArgumentParser(description="La Ruche — Agent IA Souverain")
    parser.add_argument("--no-voice",     action="store_true")
    parser.add_argument("--no-telegram",  action="store_true")
    parser.add_argument("--no-heartbeat", action="store_true")
    parser.add_argument("--cli",          action="store_true")
    parser.add_argument("--test",         action="store_true")
    args = parser.parse_args()

    print("╔═══════════════════════════════════════════╗")
    print("║        LA RUCHE — AGENT SOUVERAIN         ║")
    print("║   Spécialiste universel · 24/7 · Local    ║")
    print("╚═══════════════════════════════════════════╝\n")

    print("Vérification des dépendances...")
    if not await health_check():
        print("\n❌ Dépendances critiques manquantes.")
        sys.exit(1)
    if args.test:
        print("\n✅ Tous les services OK.")
        return

    redis = await aioredis.from_url(CFG.REDIS)
    tasks = []

    # Agent principal (toujours)
    from agent import RucheAgent
    ag = RucheAgent()
    tasks.append(asyncio.create_task(ag.start(), name="agent"))

    # Heartbeat
    if not args.no_heartbeat:
        from heartbeat import HeartbeatService
        tasks.append(asyncio.create_task(
            HeartbeatService().start(redis), name="heartbeat"))

    # Telegram
    if not args.no_telegram and CFG.TG_ENABLED:
        from senses.telegram import TelegramSense
        tasks.append(asyncio.create_task(
            TelegramSense().start(redis), name="telegram"))
    elif not CFG.TG_ENABLED:
        print("[Main] Telegram: token absent")

    # Voix
    if not args.no_voice:
        from senses.voice import VoiceSense
        tasks.append(asyncio.create_task(
            VoiceSense().start(redis), name="voice"))

    # CLI
    if args.cli:
        tasks.append(asyncio.create_task(cli_loop(redis), name="cli"))

    print(f"\n[Main] {len(tasks)} service(s): {[t.get_name() for t in tasks]}")
    print("[Main] Ctrl+C pour arrêter.\n")

    def _stop(*_):
        for t in tasks:
            t.cancel()
    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    finally:
        await ag.stop()
        await redis.aclose()
        print("\n[Main] La Ruche arrêtée proprement.")

if __name__ == "__main__":
    asyncio.run(main())
