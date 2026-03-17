"""
core/autonomy.py — Système de calibration de l'autonomie

5 niveaux:
  1 — Observation    : log seulement, aucune action
  2 — Assisté        : propose, attend validation Telegram
  3 — Autonome+log   : agit + notifie (DÉFAUT)
  4 — Autonome total : agit silencieusement
  5 — Auto-évolution : modifie son propre code (sandboxed)

Par type d'action:
  computer_use   → niveau 2 par défaut (toujours confirmer)
  file_delete    → niveau 2
  shell          → niveau 3
  web_search     → niveau 4
  memory         → niveau 4
  maintenance    → niveau 4
"""

from enum import IntEnum
from dataclasses import dataclass, field
from pathlib import Path
import json
from typing import Optional

from config import CFG
from core.logger import get_logger

log = get_logger("autonomy")

AUTONOMY_CONFIG_FILE = Path.home() / ".ruche" / "autonomy.json"


class AutonomyLevel(IntEnum):
    OBSERVATION = 1  # Log only
    ASSISTED    = 2  # Demander confirmation
    AUTONOMOUS  = 3  # Agir + notifier (défaut)
    SILENT      = 4  # Agir silencieusement
    SELF_EVOLVE = 5  # Auto-modification (sandboxed)


# Niveaux par catégorie d'outil
DEFAULT_LEVELS: dict[str, int] = {
    "computer":  2,   # Toujours confirmer l'action sur l'écran
    "delete":    2,   # Toujours confirmer la suppression
    "shell":     3,   # Agir + notifier
    "files":     3,   # Agir + notifier
    "web":       4,   # Silencieux
    "memory":    4,   # Silencieux
    "missions":  3,   # Agir + notifier
    "system":    3,   # Agir + notifier
    "ai":        4,   # Silencieux
    "swarm":     3,   # Agir + notifier
    "default":   3,   # Défaut général
}


class AutonomyManager:
    """
    Gère le niveau d'autonomie par catégorie d'action.

    Méthodes:
        get_level(category: str) -> AutonomyLevel
        set_level(category: str, level: int)
        should_confirm(category: str) -> bool  (True si niveau <= 2)
        should_notify(category: str) -> bool   (True si niveau <= 3)
        can_self_evolve() -> bool              (True si niveau == 5)
        save() / load()                        (persistence JSON)
        summary() -> str                       (affichage lisible)
    """

    def __init__(self):
        self._levels: dict[str, int] = dict(DEFAULT_LEVELS)
        # Override global depuis CFG si défini
        cfg_level = getattr(CFG, "AUTONOMY_LEVEL", None)
        if cfg_level is None:
            import os
            cfg_level = int(os.environ.get("AUTONOMY_LEVEL", 3))
        self._global_level: int = int(cfg_level)
        self.load()

    def get_level(self, category: str) -> AutonomyLevel:
        """
        Retourne le niveau effectif pour une catégorie.
        Si le niveau global est plus restrictif que le niveau par catégorie, l'override s'applique.
        """
        level = self._levels.get(category, self._levels.get("default", 3))
        # Le niveau global ne peut qu'augmenter la restriction (abaisser le niveau)
        effective = min(level, self._global_level) if self._global_level < level else level
        return AutonomyLevel(effective)

    def set_level(self, category: str, level: int):
        """Modifie le niveau d'autonomie pour une catégorie et persiste."""
        if 1 <= level <= 5:
            self._levels[category] = level
            self.save()
            log.info("autonomy_level_changed", category=category, level=level)
        else:
            log.warning("autonomy_invalid_level", category=category, level=level)

    def should_confirm(self, category: str) -> bool:
        """True si l'action demande une confirmation humaine (niveau <= 2)."""
        return self.get_level(category) <= AutonomyLevel.ASSISTED

    def should_notify(self, category: str) -> bool:
        """True si l'action doit être notifiée (niveau <= 3)."""
        return self.get_level(category) <= AutonomyLevel.AUTONOMOUS

    def can_self_evolve(self) -> bool:
        """True si l'auto-modification est autorisée (niveau == 5 sur 'system')."""
        return self._levels.get("system", 3) >= AutonomyLevel.SELF_EVOLVE

    def save(self):
        """Persiste la configuration dans ~/.ruche/autonomy.json."""
        AUTONOMY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTONOMY_CONFIG_FILE.write_text(json.dumps(self._levels, indent=2))

    def load(self):
        """Charge la configuration persistée si elle existe."""
        if AUTONOMY_CONFIG_FILE.exists():
            try:
                saved = json.loads(AUTONOMY_CONFIG_FILE.read_text())
                if isinstance(saved, dict):
                    self._levels.update(saved)
            except Exception:
                pass  # Utilise les défauts si le fichier est corrompu

    def summary(self) -> str:
        """Retourne un affichage lisible des niveaux actuels."""
        names = {
            1: "Observation",
            2: "Assisté",
            3: "Autonome+log",
            4: "Silencieux",
            5: "Auto-évolution",
        }
        lines = [f"**Niveaux d'autonomie actuels:** (global override: {self._global_level})"]
        for cat, lvl in sorted(self._levels.items()):
            effective = self.get_level(cat)
            marker    = " ⬇" if int(effective) < lvl else ""
            lines.append(f"  • {cat}: niveau {int(effective)} — {names.get(int(effective), '?')}{marker}")
        return "\n".join(lines)


# ─── Singleton ────────────────────────────────────────────────────────────────

_autonomy: Optional[AutonomyManager] = None


def get_autonomy() -> AutonomyManager:
    global _autonomy
    if _autonomy is None:
        _autonomy = AutonomyManager()
    return _autonomy
