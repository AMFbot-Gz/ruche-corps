"""
Intégration OpenAI — génération d'images DALL-E 3, fallback LLM, transcription Whisper
Clé API depuis CFG.OPENAI_KEY (variable OPENAI_API_KEY dans ~/.ruche/.env)
"""
import base64
import json
import time
from pathlib import Path

import httpx

from config import CFG

# Dossier de sauvegarde des images générées
IMAGES_DIR = Path.home() / ".ruche" / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

OPENAI_BASE = "https://api.openai.com/v1"


def _headers() -> dict:
    """Headers d'authentification OpenAI."""
    if not CFG.OPENAI_KEY:
        raise ValueError("OPENAI_API_KEY non configurée dans ~/.ruche/.env")
    return {
        "Authorization": f"Bearer {CFG.OPENAI_KEY}",
        "Content-Type":  "application/json",
    }


async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    quality: str = "standard",
) -> str:
    """
    Génère une image avec DALL-E 3.
    size: '1024x1024' | '1792x1024' | '1024x1792'
    quality: 'standard' | 'hd'
    Retourne le path local de l'image sauvegardée.
    """
    payload = {
        "model":   "dall-e-3",
        "prompt":  prompt,
        "n":       1,
        "size":    size,
        "quality": quality,
        "response_format": "b64_json",
    }

    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{OPENAI_BASE}/images/generations",
            headers=_headers(),
            json=payload,
        )
        r.raise_for_status()
        data = r.json()

    b64 = data["data"][0]["b64_json"]
    img_bytes = base64.b64decode(b64)

    # Sauvegarde locale avec timestamp
    filename = f"dalle_{int(time.time())}.png"
    path = IMAGES_DIR / filename
    path.write_bytes(img_bytes)

    return str(path)


async def ask_gpt(prompt: str, model: str = "gpt-4o-mini") -> str:
    """
    Fallback LLM via OpenAI — utile si Ollama est down.
    model: 'gpt-4o-mini' | 'gpt-4o' | 'gpt-4-turbo'
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{OPENAI_BASE}/chat/completions",
            headers=_headers(),
            json=payload,
        )
        r.raise_for_status()
        data = r.json()

    return data["choices"][0]["message"]["content"]


async def transcribe(audio_path: str) -> str:
    """
    Transcrit un fichier audio avec Whisper API.
    audio_path: chemin vers le fichier audio (mp3, mp4, wav, m4a, webm...)
    Retourne le texte transcrit.
    """
    p = Path(audio_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Fichier audio introuvable: {audio_path}")

    # Pour l'upload de fichier, on utilise multipart (pas JSON)
    headers = {"Authorization": f"Bearer {CFG.OPENAI_KEY}"}

    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(
            f"{OPENAI_BASE}/audio/transcriptions",
            headers=headers,
            files={"file": (p.name, p.read_bytes(), "audio/mpeg")},
            data={"model": "whisper-1"},
        )
        r.raise_for_status()
        data = r.json()

    return data.get("text", "")
