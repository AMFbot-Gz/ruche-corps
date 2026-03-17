"""
core/self_repair.py — Auto-réparation de La Ruche via Claude Code CLI

Adapté depuis pico-omni-agentique/actions/self_repair.py.

SelfRepair : génère des rapports de crash + tente de corriger automatiquement
             le module fautif en appelant `claude -p`.

@watch_and_repair : décorateur qui encapsule n'importe quelle coroutine async
                    et déclenche la réparation si elle lève une exception.

Rapports : ~/.ruche/logs/crash_reports/crash_TIMESTAMP.txt
"""

import asyncio
import functools
import inspect
import subprocess
import traceback as tb_module
from datetime import datetime
from pathlib import Path

from config import CFG, RUCHE_DIR

# Répertoire de stockage des rapports de crash
CRASH_DIR = RUCHE_DIR / "logs" / "crash_reports"


# ─── SelfRepair ───────────────────────────────────────────────────────────────

class SelfRepair:
    """Génère des rapports de crash et tente l'auto-réparation via Claude Code CLI."""

    def __init__(self):
        CRASH_DIR.mkdir(parents=True, exist_ok=True)

    # ── Rapport de crash ──────────────────────────────────────────────────────

    def generate_report(self, module_path: str, error: str, tb: str) -> str:
        """
        Génère un rapport de crash dans crash_reports/.
        Inclut le contenu du fichier source pour permettre l'analyse.
        Retourne le chemin absolu du rapport créé.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        report_path = CRASH_DIR / f"crash_{timestamp}.txt"

        # Lecture best-effort du fichier source
        file_content = ""
        try:
            file_content = Path(module_path).read_text(encoding="utf-8")
        except Exception as read_err:
            file_content = f"[Impossible de lire le fichier : {read_err}]"

        content = (
            f"MODULE: {module_path}\n"
            f"ERREUR: {error}\n"
            f"TRACEBACK:\n{tb}\n"
            f"CONTENU DU FICHIER:\n{file_content}\n"
        )

        report_path.write_text(content, encoding="utf-8")
        print(f"[SelfRepair] Rapport de crash : {report_path}")
        return str(report_path)

    # ── Réparation via Claude Code CLI ───────────────────────────────────────

    def repair(self, module_path: str, error: str, tb: str) -> bool:
        """
        Génère un rapport puis appelle `claude -p` pour corriger le module.

        Retourne True si le fichier existe toujours après réparation
        (critère de succès minimal : Claude a pu modifier le fichier sans le supprimer).
        """
        report_path = self.generate_report(module_path, error, tb)

        prompt = (
            f"Répare {module_path}. "
            f"Erreur: {error}. "
            f"Traceback: {tb[:600]}. "
            "Modifie uniquement ce fichier Python. "
            "Garde toutes les interfaces existantes intactes."
        )

        ruche_root = Path(__file__).parent.parent

        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=str(ruche_root),
            )
            success = result.returncode == 0
            if not success:
                print(f"[SelfRepair] Claude Code stderr : {result.stderr[:300]}")
            else:
                print(f"[SelfRepair] Réparation terminée pour {module_path}")
        except FileNotFoundError:
            print("[SelfRepair] Claude Code CLI introuvable — npm install -g @anthropic-ai/claude-code")
            success = False
        except subprocess.TimeoutExpired:
            print("[SelfRepair] Timeout : Claude Code n'a pas répondu en 90s")
            success = False
        except Exception as e:
            print(f"[SelfRepair] Erreur subprocess : {e}")
            success = False

        return Path(module_path).exists()


# ─── Décorateur watch_and_repair ─────────────────────────────────────────────

def watch_and_repair(func):
    """
    Décorateur async qui surveille une coroutine et tente une auto-réparation
    si elle lève une exception.

    Comportement :
    1. Exécute func normalement.
    2. En cas d'exception : génère un rapport + appelle SelfRepair.repair().
    3. Si réparation signalée : retente func() une fois.
    4. Si échec définitif : log le rapport et retourne None.

    Fonctionne sur les fonctions async ET sync.

    Usage :
        @watch_and_repair
        async def ma_coroutine():
            ...
    """
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)

        except Exception as exc:
            error_str = str(exc)
            tb_str    = tb_module.format_exc()

            module_file = _resolve_module_path(func)
            print(f"\n[SelfRepair] Exception dans '{func.__name__}' : {error_str}")
            print(f"[SelfRepair] Tentative d'auto-réparation → {module_file}")

            repairer = SelfRepair()
            repaired = repairer.repair(module_file, error_str, tb_str)

            if repaired:
                print(f"[SelfRepair] Réparation signalée — nouvelle tentative de '{func.__name__}'…")
                try:
                    return await func(*args, **kwargs)
                except Exception as retry_exc:
                    final_tb = tb_module.format_exc()
                    print(f"[SelfRepair] Echec après réparation : {retry_exc}")
                    repairer.generate_report(module_file, str(retry_exc), final_tb)
                    return None
            else:
                print(f"[SelfRepair] Réparation échouée pour '{func.__name__}'.")
                return None

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)

        except Exception as exc:
            error_str = str(exc)
            tb_str    = tb_module.format_exc()

            module_file = _resolve_module_path(func)
            print(f"\n[SelfRepair] Exception dans '{func.__name__}' : {error_str}")

            repairer = SelfRepair()
            repaired = repairer.repair(module_file, error_str, tb_str)

            if repaired:
                try:
                    return func(*args, **kwargs)
                except Exception as retry_exc:
                    final_tb = tb_module.format_exc()
                    repairer.generate_report(module_file, str(retry_exc), final_tb)
                    return None
            return None

    # Choisit le wrapper selon le type de fonction
    return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper


# ─── Helper privé ─────────────────────────────────────────────────────────────

def _resolve_module_path(func) -> str:
    """Retourne le chemin absolu du fichier source d'une fonction."""
    try:
        source_file = inspect.getfile(func)
        return str(Path(source_file).resolve())
    except (TypeError, OSError):
        return f"<module inconnu : {func.__module__}.{func.__qualname__}>"
