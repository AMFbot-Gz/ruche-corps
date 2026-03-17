"""
computer/screen.py — Yeux de La Ruche
Screenshot macOS + analyse vision via Ollama llava/llama3.2-vision
"""
import asyncio
import base64
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from config import CFG

SCREEN_PATH = Path("/tmp/ruche_screen.png")
_last_hash  = ""
_http       = httpx.AsyncClient(timeout=30.0)


def screenshot(region: Optional[str] = None) -> Path:
    """Capture l'écran via screencapture macOS. Retourne le chemin du PNG."""
    if sys.platform != "darwin":
        raise RuntimeError(f"screencapture non disponible sur {sys.platform}")
    cmd = ["screencapture", "-x", str(SCREEN_PATH)]
    if region:
        cmd = ["screencapture", "-x", "-R", region, str(SCREEN_PATH)]
    result = subprocess.run(cmd, capture_output=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"screencapture failed: {result.stderr.decode()[:200]}")
    if not SCREEN_PATH.exists():
        raise RuntimeError("screencapture n'a pas produit de fichier")
    return SCREEN_PATH


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def see(question: str = "Décris ce que tu vois sur l'écran en détail.") -> dict:
    """
    Screenshot + analyse vision.
    Retourne { description, changed, screenshot_b64, timestamp }
    """
    global _last_hash
    try:
        path = screenshot()
    except Exception as e:
        return {"error": str(e), "description": "", "changed": False}

    new_hash = _hash(path)
    changed  = new_hash != _last_hash
    _last_hash = new_hash

    img_b64  = base64.b64encode(path.read_bytes()).decode()

    # Analyse vision via Ollama
    description = ""
    try:
        resp = await _http.post(
            f"{CFG.OLLAMA}/api/chat",
            json={
                "model": CFG.M_VISION,
                "messages": [{
                    "role": "user",
                    "content": question,
                    "images": [img_b64],
                }],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 800},
            },
        )
        description = resp.json().get("message", {}).get("content", "")
    except Exception as e:
        description = f"(vision non disponible: {e})"

    return {
        "description": description,
        "changed":     changed,
        "path":        str(path),
        "timestamp":   datetime.now().isoformat(),
    }


async def find_element(description: str) -> dict:
    """
    Cherche un élément visuel sur l'écran.
    Retourne { found, x, y, confidence, description }
    Utilise la vision pour décrire les coordonnées approximatives.
    """
    result = await see(
        f"Trouve '{description}' sur l'écran. "
        "Donne les coordonnées approximatives (x, y) en pixels depuis le coin supérieur gauche. "
        "Réponds en JSON: {\"found\": bool, \"x\": int, \"y\": int, \"confidence\": float, \"note\": str}"
    )
    desc = result.get("description", "")
    try:
        import re
        m = re.search(r'\{[^}]+\}', desc, re.DOTALL)
        if m:
            data = json.loads(m.group())
            return {**data, "raw": desc}
    except Exception:
        pass
    return {"found": False, "x": 0, "y": 0, "confidence": 0.0, "raw": desc}
