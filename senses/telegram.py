"""
senses/telegram.py — Canal Telegram de La Ruche
Reçoit les messages → Redis inbound
Écoute Redis outbound → envoie les réponses

S'abonne aussi au heartbeat pour alertes automatiques.
"""
import asyncio
import json
import logging
import time

import httpx
import redis.asyncio as aioredis

log = logging.getLogger("ruche.telegram")
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters

from config import CFG

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# Timestamp de démarrage du processus (pour calculer l'uptime)
_START_TIME = time.monotonic()


class TelegramSense:
    def __init__(self):
        self.redis = None
        self._app  = None
        self._bot  = None
        # Map session_id → chat_id pour renvoyer les réponses
        self._chat_map: dict[str, int] = {}

    async def start(self, redis_client=None):
        if not CFG.TG_ENABLED:
            print("[Telegram] Pas de token configuré, canal désactivé.")
            return

        self.redis = redis_client or await aioredis.from_url(CFG.REDIS)
        self._app  = Application.builder().token(CFG.TG_TOKEN).build()
        self._bot  = self._app.bot

        # Handlers
        self._app.add_handler(CommandHandler("start",    self._cmd_start))
        self._app.add_handler(CommandHandler("status",   self._cmd_status))
        self._app.add_handler(CommandHandler("clear",    self._cmd_clear))
        self._app.add_handler(CommandHandler("models",   self._cmd_models))
        self._app.add_handler(CommandHandler("memory",   self._cmd_memory))
        self._app.add_handler(CommandHandler("autonomy", self._cmd_autonomy))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

        print(f"[Telegram] Canal démarré. Admin: {CFG.TG_ADMIN}")

        # Tâches parallèles : recevoir et envoyer
        await asyncio.gather(
            self._start_polling(),
            self._outbound_listener(),
            self._heartbeat_listener(),
        )

    # ─── Réception messages ────────────────────────────────────────────────
    async def _on_message(self, update: Update, context):
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id
        text    = update.message.text

        # Vérification admin
        if CFG.TG_ADMIN and user_id != str(CFG.TG_ADMIN):
            await update.message.reply_text("Accès non autorisé.")
            return

        session_id = f"telegram:{user_id}"
        self._chat_map[session_id] = chat_id

        # Indicateur de frappe
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Publier sur Redis pour le Corps
        await self.redis.publish(CFG.CH_IN, json.dumps({
            "channel":    "telegram",
            "user_id":    user_id,
            "chat_id":    chat_id,
            "text":       text,
            "session_id": session_id,
        }))

    async def _cmd_start(self, update: Update, context):
        await update.message.reply_text(
            "🦾 *La Ruche — Corps actif*\n\n"
            f"Je suis {CFG.NAME}, votre assistant IA personnel.\n"
            "Envoyez-moi un message pour commencer.\n\n"
            "Commandes : /status /models /memory /clear /autonomy",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context):
        """État complet de la Ruche — construit localement depuis Redis + Ollama."""
        try:
            # Uptime
            uptime_sec = int(time.monotonic() - _START_TIME)
            uptime_h   = uptime_sec // 3600
            uptime_m   = (uptime_sec % 3600) // 60
            uptime_str = f"{uptime_h}h{uptime_m:02d}m" if uptime_h else f"{uptime_m}m"

            # Nombre d'outils (import lazy pour éviter dépendance circulaire)
            try:
                from tools.registry import registry
                n_tools = len(registry.list_tools())
            except Exception:
                n_tools = 0

            # Mémoire ChromaDB — compter les épisodes
            mem_count = 0
            chroma_status = "❌"
            try:
                import chromadb
                from pathlib import Path
                chroma_path = Path.home() / ".ruche" / "memory" / "chroma"
                client = chromadb.PersistentClient(path=str(chroma_path))
                try:
                    col = client.get_collection("ruche_episodes")
                    mem_count = col.count()
                    chroma_status = "✅"
                except Exception:
                    chroma_status = "✅"
            except Exception:
                chroma_status = "❌"

            # Worker — missions en attente dans Redis
            worker_pending = 0
            try:
                queue_len = await self.redis.llen("ruche:missions:queue")
                worker_pending = int(queue_len or 0)
            except Exception:
                pass

            # Goals — en attente dans SQLite
            goals_pending = 0
            try:
                import sqlite3
                from pathlib import Path
                goals_db = Path.home() / ".ruche" / "goals.db"
                if goals_db.exists():
                    conn = sqlite3.connect(str(goals_db))
                    row = conn.execute(
                        "SELECT COUNT(*) FROM goals WHERE status='pending'"
                    ).fetchone()
                    conn.close()
                    goals_pending = row[0] if row else 0
            except Exception:
                pass

            # Services — ping rapide
            async def _ping(url: str) -> str:
                try:
                    async with httpx.AsyncClient(timeout=2.0) as c:
                        r = await c.get(url)
                        return "✅" if r.status_code < 500 else "❌"
                except Exception:
                    return "❌"

            redis_ok   = "✅"  # si on est ici, Redis fonctionne
            ollama_ok  = await _ping(f"{CFG.OLLAMA}/api/tags")
            n8n_ok     = await _ping(f"{CFG.N8N_URL}/healthz")
            pg_ok      = "N/A"

            lines = [
                f"🧠 *La Ruche — En ligne depuis {uptime_str}*",
                f"📊 {n_tools} outils · `{CFG.M_GENERAL}`",
                f"💾 Mémoire: {mem_count} souvenirs · ChromaDB {chroma_status}",
                f"⚙️  Worker: {worker_pending} missions en attente",
                f"🎯 Goals: {goals_pending} en attente",
                f"🔧 Services: Redis{redis_ok} Ollama{ollama_ok} N8N{n8n_ok}",
            ]
            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"Erreur /status : {e}")

    async def _cmd_clear(self, update: Update, context):
        user_id    = str(update.effective_user.id)
        session_id = f"telegram:{user_id}"
        await self.redis.delete(f"ruche:session:{session_id}")
        await update.message.reply_text("Historique effacé. Nouvelle conversation.")

    async def _cmd_models(self, update: Update, context):
        """Affiche les modèles Ollama actifs pour chaque rôle."""
        lines = [
            "🤖 *Modèles actifs:*",
            f"  General : `{CFG.M_GENERAL}`",
            f"  Code    : `{CFG.M_CODE}`",
            f"  Fast    : `{CFG.M_FAST}`",
            f"  Vision  : `{CFG.M_VISION}`",
            f"  Router  : `{CFG.M_ROUTER}`",
            f"  Embed   : `{CFG.M_EMBED}`",
        ]
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    async def _cmd_memory(self, update: Update, context):
        """Résumé de l'état de la mémoire ChromaDB."""
        episodes  = 0
        facts     = 0
        rules     = 0
        try:
            import chromadb
            from pathlib import Path
            chroma_path = Path.home() / ".ruche" / "memory" / "chroma"
            client = chromadb.PersistentClient(path=str(chroma_path))
            try:
                episodes = client.get_collection("ruche_episodes").count()
            except Exception:
                pass
            try:
                facts = client.get_collection("ruche_knowledge").count()
            except Exception:
                pass
            try:
                rules = client.get_collection("ruche_procedural").count()
            except Exception:
                pass
        except Exception:
            pass

        lines = [
            "📚 *Mémoire:*",
            f"  Épisodes       : {episodes} souvenirs",
            f"  Faits          : {facts} mémorisés",
            f"  Règles apprises: {rules}",
        ]
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    async def _cmd_autonomy(self, update: Update, context):
        """Affiche le niveau d'autonomie actuel de l'agent."""
        import os
        level = int(os.getenv("AUTONOMY_LEVEL", "3"))
        descriptions = {
            1: "Demande confirmation pour chaque action",
            2: "Demande confirmation pour les actions risquées",
            3: "Autonome sauf confiance < 60% ou risques multiples",
            4: "Entièrement autonome — ne demande jamais confirmation",
            5: "Superintendance — mode nuit total",
        }
        desc = descriptions.get(level, "Niveau inconnu")
        lines = [
            "🎛️  *Niveaux d'autonomie:*",
            "",
            f"  Niveau actuel : *{level}/5*",
            f"  Mode          : {desc}",
            "",
            "  1 — Confirmation systématique",
            "  2 — Confirmation si risque",
            "  3 — Autonome (défaut)",
            "  4 — Entièrement autonome",
            "  5 — Mode nuit / superintendance",
        ]
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    # ─── Envoi des réponses ────────────────────────────────────────────────
    async def _outbound_listener(self):
        """Écouter Redis outbound et envoyer les réponses Telegram."""
        async with self.redis.pubsub() as pubsub:
            await pubsub.subscribe(CFG.CH_OUT)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data       = json.loads(message["data"])
                    channel    = data.get("channel")
                    session_id = data.get("session_id", "")
                    response   = data.get("response", "")

                    if channel != "telegram" or not response:
                        continue

                    chat_id = self._chat_map.get(session_id)
                    if not chat_id:
                        continue

                    # Découper si trop long (limite Telegram 4096 chars)
                    for i in range(0, len(response), 4000):
                        chunk = response[i:i+4000]
                        await self._bot.send_message(
                            chat_id=chat_id,
                            text=chunk,
                            parse_mode="Markdown",
                        )
                except Exception as e:
                    print(f"[Telegram] Erreur envoi: {e}")

    # ─── Alertes heartbeat ────────────────────────────────────────────────
    async def _heartbeat_listener(self):
        """Recevoir les alertes heartbeat et les envoyer à l'admin."""
        if not CFG.TG_ADMIN:
            return
        admin_chat_id = int(CFG.TG_ADMIN)
        async with self.redis.pubsub() as pubsub:
            await pubsub.subscribe(CFG.CH_HB)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data  = json.loads(message["data"])
                    level = data.get("level", "info")
                    text  = data.get("message", "")
                    if text:
                        emoji = {"warn": "⚠️", "error": "🔴", "info": "ℹ️", "briefing": "🌅"}.get(level, "•")
                        await self._bot.send_message(
                            chat_id=admin_chat_id,
                            text=f"{emoji} {text}",
                        )
                except Exception as e:
                    print(f"[Telegram] Erreur heartbeat: {e}")

    async def _start_polling(self):
        await self._app.initialize()
        await self._app.start()
        # Retry en cas de conflit (autre instance en cours d'arrêt)
        polling_started = False
        last_exc = None
        for attempt in range(10):
            try:
                await self._app.updater.start_polling(drop_pending_updates=True)
                polling_started = True
                break
            except Exception as e:
                last_exc = e
                if "Conflict" in str(e) and attempt < 9:
                    print(f"[Telegram] Conflit token (tentative {attempt+1}/10) — attente 8s...")
                    await asyncio.sleep(8)
                else:
                    print(f"[Telegram] Polling échoué: {e}")
                    break
        if not polling_started:
            log.critical(
                "[Telegram] Impossible de démarrer le polling après 10 tentatives. "
                f"Dernière erreur: {last_exc}"
            )
            raise RuntimeError(
                f"Telegram polling failed après 10 tentatives: {last_exc}"
            )
        # Garder en vie
        while True:
            await asyncio.sleep(3600)
