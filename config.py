"""
config.py — Source unique de vérité pour La Ruche
Lit ~/.ruche/.env, jamais de secrets en dur.
"""
import os
from pathlib import Path

ENV_FILE = Path.home() / ".ruche" / ".env"

def _load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

def _e(key, default=""):
    return os.environ.get(key, default)

# ─── Chemins persistants ──────────────────────────────────────
RUCHE_DIR = Path.home() / ".ruche"
for _d in ["memory/chroma", "sessions", "logs", "workspace"]:
    (RUCHE_DIR / _d).mkdir(parents=True, exist_ok=True)

# ─── Config exportée ─────────────────────────────────────────
class CFG:
    # Telegram
    TG_TOKEN   = _e("TELEGRAM_BOT_TOKEN")
    TG_ADMIN   = _e("TELEGRAM_ADMIN_ID")
    TG_ENABLED = bool(_e("TELEGRAM_BOT_TOKEN"))

    # Ghost OS (optionnel)
    GHOST_URL  = _e("GHOST_QUEEN_URL",  "http://localhost:3000")
    GHOST_CU   = _e("GHOST_CU_URL",     "http://localhost:8015")
    GHOST_SEC  = _e("GHOST_SECRET")

    # Ollama
    OLLAMA     = _e("OLLAMA_URL", "http://localhost:11434")

    # ── Modèles ───────────────────────────────────────────────
    # Router : ultra-rapide, local, < 100ms
    M_ROUTER   = "llama3.2:3b"
    # Nemotron-3-Super : raisonnement général + contexte 1M tokens
    M_GENERAL  = "nemotron-3-super:cloud"
    # Code : Qwen3-Coder 480B cloud
    M_CODE     = "qwen3-coder:480b-cloud"
    # Vision : analyse écran + images
    M_VISION   = "llama3.2-vision:latest"
    # Raisonnement difficile (math, stratégie)
    M_REASON   = "nemotron-3-super:cloud"
    # Réponses rapides (salutations, date, heure)
    M_FAST     = "llama3.2:3b"
    # Embeddings mémoire
    M_EMBED    = "nomic-embed-text:latest"

    # Paramètres Nemotron (contexte maxi)
    NEMOTRON_CTX  = 131072   # 128K tokens via Ollama (max supporté localement)
    NEMOTRON_TEMP = 0.6      # température recommandée pour Nemotron-Super

    # Redis
    REDIS      = "redis://localhost:6379"
    CH_IN      = "ruche:inbound"
    CH_OUT     = "ruche:outbound"
    CH_STREAM  = "ruche:stream"
    CH_HB      = "ruche:heartbeat"

    # Identité
    OWNER      = _e("RUCHE_OWNER", "patron")
    NAME       = _e("RUCHE_NAME",  "Jarvis")
    VOICE      = _e("RUCHE_VOICE", "Daniel")
    DEBUG      = _e("RUCHE_DEBUG", "false").lower() == "true"

    # APIs cloud
    OPENAI_KEY = _e("OPENAI_API_KEY")
    GITHUB_TK  = _e("GITHUB_TOKEN")
