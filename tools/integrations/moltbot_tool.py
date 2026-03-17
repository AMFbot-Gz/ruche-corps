"""
tools/integrations/moltbot_tool.py — Intégration Clawdbot (clawd.bot)

Clawdbot est un gateway CLI cross-platform (WhatsApp/Telegram/Discord/iMessage/Signal)
basé sur Node.js. Il expose un port HTTP sur 18789 (gateway) et peut être
interrogé via sa CLI (`clawdbot`).

Architecture découverte dans ~/Projects/moltbot :
  - Gateway HTTP sur port 18789 (token CLAWDBOT_GATEWAY_TOKEN)
  - CLI : `clawdbot gateway run`, `clawdbot channels status`, `clawdbot message send`
  - Config : ~/.clawdbot/ (credentials, sessions, agents)
  - Docker : clawdbot-gateway + clawdbot-cli
  - Démon sur macOS via launchd (restart via mac app ou scripts/restart-mac.sh)
"""
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import httpx

# ─── Constantes ─────────────────────────────────────────────────────────────

MOLTBOT_DIR       = Path.home() / "Projects" / "moltbot"
CLAWDBOT_DIR      = Path.home() / ".clawdbot"
GATEWAY_PORT      = int(os.environ.get("CLAWDBOT_GATEWAY_PORT", "18789"))
GATEWAY_TOKEN     = os.environ.get("CLAWDBOT_GATEWAY_TOKEN", "")
GATEWAY_BASE      = f"http://localhost:{GATEWAY_PORT}"

# Headers d'authentification pour le gateway HTTP
def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if GATEWAY_TOKEN:
        h["Authorization"] = f"Bearer {GATEWAY_TOKEN}"
    return h


# ─── CLI helper ─────────────────────────────────────────────────────────────

def _run_cli(*args: str, timeout: int = 15) -> dict:
    """
    Exécute une commande clawdbot via le binaire dist/entry.js du projet local,
    ou via `clawdbot` si installé globalement.
    Retourne {"stdout": str, "stderr": str, "ok": bool}.
    """
    # Chercher le binaire dans l'ordre de préférence
    candidates = [
        str(MOLTBOT_DIR / "dist" / "entry.js"),   # build local
        "clawdbot",                                 # installé globalement (npm -g)
    ]
    bin_path = None
    for c in candidates:
        if c == "clawdbot":
            # Vérifier dans PATH
            if subprocess.run(["which", "clawdbot"], capture_output=True).returncode == 0:
                bin_path = "clawdbot"
                break
        elif Path(c).exists():
            bin_path = f"node {c}"
            break

    if bin_path is None:
        return {
            "stdout": "",
            "stderr": "Clawdbot non disponible (dist/ non buildé et non installé globalement)",
            "ok": False,
        }

    cmd_str = f"{bin_path} {' '.join(args)}"
    try:
        r = subprocess.run(
            cmd_str,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(MOLTBOT_DIR),
        )
        return {
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
            "ok": r.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timeout ({timeout}s)", "ok": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "ok": False}


# ─── Fonctions publiques ─────────────────────────────────────────────────────

async def get_status() -> dict:
    """
    Retourne le statut de Clawdbot :
    - gateway_running : bool (port 18789 accessible)
    - version : str (depuis package.json)
    - config_dir_exists : bool (~/.clawdbot/)
    - cli_available : bool
    - channels : liste des canaux actifs si gateway disponible
    """
    result = {
        "gateway_running": False,
        "version": "?",
        "config_dir_exists": CLAWDBOT_DIR.exists(),
        "cli_available": False,
        "channels": [],
        "gateway_url": GATEWAY_BASE,
    }

    # Lire la version depuis package.json
    pkg = MOLTBOT_DIR / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            result["version"] = data.get("version", "?")
        except Exception:
            pass

    # Tester si le gateway HTTP répond
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{GATEWAY_BASE}/health", headers=_headers())
            result["gateway_running"] = r.status_code < 500
    except Exception:
        # Tenter /api/health comme fallback
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{GATEWAY_BASE}/api/health", headers=_headers())
                result["gateway_running"] = r.status_code < 500
        except Exception:
            result["gateway_running"] = False

    # Vérifier si la CLI est disponible
    r_cli = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _run_cli("--version")
    )
    result["cli_available"] = r_cli["ok"]
    if r_cli["ok"] and r_cli["stdout"]:
        # La CLI retourne la version
        result["version"] = r_cli["stdout"].split("\n")[0].strip() or result["version"]

    return result


async def get_channels_status() -> str:
    """
    Retourne le statut des canaux Clawdbot via `clawdbot channels status`.
    Equivalent de : clawdbot channels status --probe
    """
    r = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _run_cli("channels", "status")
    )
    if r["ok"]:
        return r["stdout"] or "(aucune sortie)"
    return f"Erreur: {r['stderr']}"


async def get_config() -> dict:
    """
    Lit la configuration Clawdbot depuis ~/.clawdbot/.
    Retourne les fichiers de config disponibles sans exposer les secrets.
    """
    config_info = {
        "config_dir": str(CLAWDBOT_DIR),
        "exists": CLAWDBOT_DIR.exists(),
        "files": [],
        "sessions_count": 0,
        "agents_count": 0,
    }

    if not CLAWDBOT_DIR.exists():
        return config_info

    # Lister les fichiers de config (sans lire les credentials)
    safe_patterns = ["*.json", "*.yaml", "*.yml"]
    ignore_names  = {"credentials", "session", "token", "secret", "key"}

    for f in CLAWDBOT_DIR.rglob("*"):
        if not f.is_file():
            continue
        # Ignorer les fichiers sensibles
        if any(w in f.name.lower() for w in ignore_names):
            continue
        try:
            rel = f.relative_to(CLAWDBOT_DIR)
            config_info["files"].append(str(rel))
        except ValueError:
            pass

    # Compter les sessions
    sessions_dir = CLAWDBOT_DIR / "sessions"
    if sessions_dir.exists():
        config_info["sessions_count"] = len(list(sessions_dir.iterdir()))

    # Compter les agents
    agents_dir = CLAWDBOT_DIR / "agents"
    if agents_dir.exists():
        config_info["agents_count"] = len([
            d for d in agents_dir.iterdir() if d.is_dir()
        ])

    return config_info


async def list_conversations() -> str:
    """
    Liste les conversations/sessions actives via la CLI clawdbot.
    """
    r = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _run_cli("status", "--all")
    )
    if r["ok"]:
        return r["stdout"] or "(aucune conversation active)"
    return f"CLI non disponible: {r['stderr']}"


async def restart() -> str:
    """
    Redémarre le gateway Clawdbot via le script macOS prévu.
    Utilise scripts/restart-mac.sh du projet.
    """
    restart_script = MOLTBOT_DIR / "scripts" / "restart-mac.sh"
    if restart_script.exists():
        r = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["bash", str(restart_script)],
                capture_output=True, text=True, timeout=30,
                cwd=str(MOLTBOT_DIR),
            )
        )
        if r.returncode == 0:
            return f"Gateway redémarré.\n{r.stdout.strip()}"
        return f"Erreur redémarrage: {r.stderr.strip()}"

    return (
        "Script restart-mac.sh introuvable. "
        "Pour redémarrer manuellement : ouvrir l'app Clawdbot macOS "
        "ou executer `pnpm mac:restart` dans ~/Projects/moltbot/"
    )


async def send_message(channel: str, recipient: str, text: str) -> str:
    """
    Envoie un message via la CLI clawdbot.
    channel: 'whatsapp' | 'telegram' | 'discord' | 'signal' | 'imessage'
    recipient: numéro/username/ID selon le canal
    text: texte du message
    """
    r = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: _run_cli(
            "message", "send",
            "--channel", channel,
            "--to", recipient,
            "--message", text,
        )
    )
    if r["ok"]:
        return f"Message envoyé sur {channel} à {recipient}"
    return f"Erreur envoi: {r['stderr']}"


async def get_project_info() -> dict:
    """
    Retourne les infos du projet moltbot (version, scripts, plateformes supportées).
    """
    pkg = MOLTBOT_DIR / "package.json"
    if not pkg.exists():
        return {"error": f"package.json introuvable dans {MOLTBOT_DIR}"}

    data = json.loads(pkg.read_text())
    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "description": data.get("description"),
        "main_scripts": {
            k: v for k, v in data.get("scripts", {}).items()
            if k in {"dev", "start", "build", "gateway:dev", "mac:restart",
                     "test", "gateway:dev:reset"}
        },
        "platforms": ["macOS", "iOS", "Android", "WhatsApp", "Telegram",
                      "Discord", "Slack", "Signal", "iMessage"],
        "gateway_port": GATEWAY_PORT,
        "docs": "https://docs.clawd.bot",
        "built": (MOLTBOT_DIR / "dist" / "entry.js").exists(),
    }
