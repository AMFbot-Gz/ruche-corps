"""
core/model_selector.py — Sélection automatique des modèles Ollama disponibles

Au démarrage, scanne les modèles Ollama disponibles et sélectionne
automatiquement le meilleur pour chaque rôle (general, code, fast, vision).

Logique de sélection par ordre de préférence:
- general/reason: nemotron-3-super > qwen3 > llama3.3 > llama3.1 > llama3.2
- code: qwen3-coder > deepseek-coder > codellama > qwen3 > general
- fast: llama3.2:3b > llama3.2:1b > llama3.1:8b > le plus petit modèle dispo
- vision: llama3.2-vision > llava > minicpm-v > general (sans vision)
- embed: nomic-embed-text > mxbai-embed > all-minilm > None
"""

import httpx
import asyncio
from config import CFG
from core.logger import get_logger

log = get_logger("model_selector")

async def list_available_models() -> list[str]:
    """Retourne la liste des modèles Ollama disponibles."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get(f"{CFG.OLLAMA}/api/tags")
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []

def select_model(available: list[str], role: str) -> str:
    """Sélectionne le meilleur modèle pour un rôle donné."""
    # Préférences par rôle (ordre décroissant de qualité)
    preferences = {
        "general": ["nemotron-3-super", "qwen3:72b", "qwen3:32b", "qwen3", "llama3.3", "llama3.1:70b", "llama3.1", "llama3.2", "mistral"],
        "code":    ["qwen3-coder:480b", "qwen3-coder", "deepseek-coder-v2", "deepseek-coder", "codellama:70b", "codellama", "qwen3", "nemotron"],
        "fast":    ["llama3.2:3b", "llama3.2:1b", "llama3.1:8b", "qwen3:4b", "qwen3:1.7b", "phi3:mini", "gemma:2b"],
        "vision":  ["llama3.2-vision", "llava:34b", "llava:13b", "llava", "minicpm-v", "moondream"],
        "embed":   ["nomic-embed-text", "mxbai-embed-large", "all-minilm", "snowflake-arctic-embed"],
    }

    prefs = preferences.get(role, preferences["general"])
    available_lower = {m.lower(): m for m in available}

    for pref in prefs:
        for avail_lower, avail_orig in available_lower.items():
            if pref.lower() in avail_lower:
                return avail_orig

    # Fallback: premier modèle disponible (pas embed)
    non_embed = [m for m in available if "embed" not in m.lower()]
    return non_embed[0] if non_embed else (available[0] if available else CFG.M_GENERAL)

async def auto_configure_models() -> dict[str, str]:
    """
    Scanne Ollama et retourne la config optimale pour chaque rôle.
    Met à jour CFG dynamiquement si possible.
    """
    available = await list_available_models()
    if not available:
        log.warning("no_models_found", fallback="using CFG defaults")
        return {}

    selected = {
        "M_GENERAL": select_model(available, "general"),
        "M_CODE":    select_model(available, "code"),
        "M_FAST":    select_model(available, "fast"),
        "M_VISION":  select_model(available, "vision"),
    }

    # Mettre à jour CFG dynamiquement
    for attr, model in selected.items():
        if hasattr(CFG, attr):
            current = getattr(CFG, attr)
            if current != model:
                setattr(CFG, attr, model)
                log.info("model_auto_selected", role=attr, model=model, previous=current)
            else:
                log.info("model_confirmed", role=attr, model=model)

    return selected
