"""
senses/voice.py — Canal vocal de La Ruche
STT : faster-whisper → texte → Redis inbound
TTS : écoute Redis outbound → macOS say
"""
import asyncio
import json
import os
import re
import subprocess
import tempfile
import wave

import redis.asyncio as aioredis
import speech_recognition as sr

from config import CFG

WAKE_WORDS = ["jarvis", "ruche", "hey jarvis"]
STT_MODEL  = "base"
LANGUAGE   = "fr"


class VoiceSense:
    def __init__(self):
        self.redis     = None
        self._whisper  = None
        self._speaking = False
        self._user_id  = "voice:local"
        self._session  = f"voice:{os.getpid()}"

    async def start(self, redis_client=None):
        self.redis = redis_client or await aioredis.from_url(CFG.REDIS)

        await asyncio.to_thread(self._load_whisper)

        print(f"[Voice] ✅ Whisper {STT_MODEL} chargé. Wake words: {WAKE_WORDS}")
        print(f"[Voice] Voix TTS: {CFG.VOICE}")

        await asyncio.to_thread(self._speak, f"La Ruche est en ligne, {CFG.OWNER}.")

        await asyncio.gather(
            self._listen_loop(),
            self._tts_listener(),
        )

    # ─── STT : microphone → Redis ──────────────────────────────────────────
    def _load_whisper(self):
        from faster_whisper import WhisperModel
        self._whisper = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")

    def _transcribe(self, audio_data: sr.AudioData) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(audio_data.get_raw_data(convert_rate=16000, convert_width=2))
        try:
            segs, _ = self._whisper.transcribe(wav_path, language=LANGUAGE, beam_size=5)
            return " ".join(s.text for s in segs).strip()
        finally:
            if 'wav_path' in locals() and wav_path and os.path.exists(wav_path):
                try:
                    os.unlink(wav_path)
                except Exception:
                    pass

    async def _listen_loop(self):
        recognizer = sr.Recognizer()
        recognizer.energy_threshold        = 300
        recognizer.dynamic_energy_threshold = True
        recognizer.pause_threshold         = 0.8
        mic = sr.Microphone(sample_rate=16000)

        print("[Voice] Calibration micro...", end=" ", flush=True)
        with mic as source:
            recognizer.adjust_for_ambient_noise(source, duration=1)
        print("✅")
        print(f"[Voice] En écoute — dites {WAKE_WORDS[0]}...")

        while True:
            try:
                text = await asyncio.to_thread(self._listen_once, recognizer, mic)
                if not text:
                    continue

                text_lower = text.lower()
                detected   = False
                command    = text

                for ww in WAKE_WORDS:
                    if ww in text_lower:
                        idx     = text_lower.find(ww) + len(ww)
                        command = text[idx:].strip(" ,.")
                        detected = True
                        break

                if not detected:
                    continue

                print(f"[Voice] Wake word! Commande: {command!r}")

                if not command:
                    await asyncio.to_thread(self._speak, f"Oui, {CFG.OWNER} ?")
                    command = await asyncio.to_thread(self._listen_once, recognizer, mic, timeout=8)
                    if not command:
                        await asyncio.to_thread(self._speak, "Je n'ai pas entendu.")
                        continue

                await self.redis.publish(CFG.CH_IN, json.dumps({
                    "channel":    "voice",
                    "user_id":    self._user_id,
                    "text":       command,
                    "session_id": self._session,
                }))

            except Exception as e:
                print(f"[Voice] Erreur écoute: {e}")
                await asyncio.sleep(1)

    def _listen_once(self, recognizer, mic, timeout=3) -> str | None:
        try:
            with mic as source:
                audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=10)
            return self._transcribe(audio)
        except sr.WaitTimeoutError:
            return None
        except Exception:
            return None

    # ─── TTS : Redis outbound → voix ───────────────────────────────────────
    async def _tts_listener(self):
        async with self.redis.pubsub() as pubsub:
            await pubsub.subscribe(CFG.CH_OUT, CFG.CH_HB)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data    = json.loads(message["data"])
                    channel = data.get("channel", data.get("type", ""))
                    if channel in ("voice", "heartbeat_alert"):
                        text = data.get("response") or data.get("message") or ""
                        if text:
                            asyncio.create_task(asyncio.to_thread(self._speak, text))
                except Exception:
                    pass

    def _speak(self, text: str):
        if self._speaking:
            return
        clean = re.sub(r"[*_`#\[\]()]", "", text)
        clean = re.sub(r"https?://\S+", "lien web", clean)
        clean = re.sub(r"[^\w\s',\.\!\?\;\:\-]", "", clean)
        clean = clean.strip()
        if not clean:
            return
        self._speaking = True
        try:
            subprocess.run(["say", "-v", CFG.VOICE, clean], capture_output=True, timeout=60)
        finally:
            self._speaking = False
