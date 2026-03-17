"""
senses/telegram.py — Canal Telegram de La Ruche
Reçoit les messages → Redis inbound
Écoute Redis outbound → envoie les réponses

S'abonne aussi au heartbeat pour alertes automatiques.
"""
import asyncio
import json
import logging

import redis.asyncio as aioredis

log = logging.getLogger("ruche.telegram")
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters

from config import CFG

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


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
        self._app.add_handler(CommandHandler("start",  self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("clear",  self._cmd_clear))
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
            "Commandes : /status /clear",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context):
        user_id = str(update.effective_user.id)
        session_id = f"telegram:{user_id}"
        self._chat_map[session_id] = update.effective_chat.id
        await self.redis.publish(CFG.CH_IN, json.dumps({
            "channel": "telegram",
            "user_id": user_id,
            "text":    "Donne-moi le statut complet du système",
            "session_id": session_id,
        }))

    async def _cmd_clear(self, update: Update, context):
        user_id    = str(update.effective_user.id)
        session_id = f"telegram:{user_id}"
        await self.redis.delete(f"ruche:session:{session_id}")
        await update.message.reply_text("Historique effacé. Nouvelle conversation.")

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
