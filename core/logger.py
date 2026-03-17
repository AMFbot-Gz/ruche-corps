"""
core/logger.py — Logging structuré pour La Ruche

Utilise structlog pour produire :
- JSON en production (LOG_LEVEL != DEBUG)
- ConsoleRenderer coloré en développement (LOG_LEVEL == DEBUG)

Chaque logger bind automatiquement service_name et version.
"""
import logging
import sys

import structlog

from config import CFG


# ─── Détection du mode (dev vs prod) ─────────────────────────────────────────
_DEV = CFG.LOG_LEVEL.upper() == "DEBUG"

# ─── Configuration logging stdlib (requis par structlog) ──────────────────────
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=getattr(logging, CFG.LOG_LEVEL.upper(), logging.INFO),
)

# ─── Processeurs partagés (appliqués dans l'ordre) ────────────────────────────
_shared_processors = [
    # Ajoute timestamp ISO8601
    structlog.processors.TimeStamper(fmt="iso"),
    # Ajoute le niveau de log
    structlog.stdlib.add_log_level,
    # Ajoute le nom du logger
    structlog.stdlib.add_logger_name,
    # Filtre selon LOG_LEVEL configuré
    structlog.stdlib.filter_by_level,
    # Formate les exceptions en stack trace lisible
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

# ─── Renderer final : JSON en prod, couleurs en dev ───────────────────────────
if _DEV:
    _renderer = structlog.dev.ConsoleRenderer(colors=True)
else:
    _renderer = structlog.processors.JSONRenderer()

# ─── Configuration globale structlog ──────────────────────────────────────────
structlog.configure(
    processors=_shared_processors + [_renderer],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Retourne un BoundLogger préconfiguré.
    Bind automatiquement service_name et version pour traçabilité.

    Usage:
        log = get_logger(__name__)
        log.info("message", session_id="xxx", ms=42)
    """
    return structlog.get_logger(name).bind(
        service_name="ruche-corps",
        version="2.0",
    )
