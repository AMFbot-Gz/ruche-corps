"""
core/resilience.py — Résilience réseau pour La Ruche

OllamaClient : wrapper httpx avec :
  - Circuit breaker (pybreaker) : coupe après 5 échecs, reset après 30s
  - Backoff exponentiel : 1s → 2s → 4s → 8s → 16s avec jitter aléatoire
  - Logging structuré de chaque retry, open, close

Singleton via get_ollama_client() — une seule instance partagée.
"""
import asyncio
import random

import httpx
import pybreaker

from config import CFG
from core.logger import get_logger

log = get_logger(__name__)


# ─── Exception personnalisée ──────────────────────────────────────────────────
class CircuitOpenError(Exception):
    """Levée quand le circuit breaker est ouvert (Ollama considéré HS)."""
    pass


# ─── Listener pour logger les transitions d'état ─────────────────────────────
class _BreakerListener(pybreaker.CircuitBreakerListener):
    """Log chaque changement d'état du circuit breaker."""

    def state_change(self, cb, old_state, new_state):
        log.warning(
            "circuit_breaker_state_change",
            breaker=cb.name,
            old_state=str(old_state),
            new_state=str(new_state),
        )

    def failure(self, cb, exc):
        log.error(
            "circuit_breaker_failure",
            breaker=cb.name,
            fail_count=cb.fail_counter,
            error=str(exc),
        )

    def success(self, cb):
        log.debug(
            "circuit_breaker_success",
            breaker=cb.name,
        )


# ─── OllamaClient ────────────────────────────────────────────────────────────
class OllamaClient:
    """
    Client HTTP résilient pour Ollama.
    Chaque appel .chat() est protégé par un circuit breaker et un backoff.

    pybreaker fonctionne en mode synchrone ; on gère le comptage d'erreurs
    manuellement via _record_success() / _record_failure() pour rester 100% async.
    """

    # Délais de backoff exponentiel (secondes)
    _BACKOFF_DELAYS = [1, 2, 4, 8, 16]

    def __init__(self):
        self._fail_count    = 0
        self._fail_max      = 5
        self._reset_timeout = 30        # secondes
        self._open_since    = None      # timestamp float quand le circuit s'ouvre
        self._state         = "closed"  # "closed" | "open" | "half-open"

    # ── Gestion d'état ────────────────────────────────────────────────────────
    def _is_open(self) -> bool:
        if self._state == "closed":
            return False
        if self._state == "open":
            # Vérifie si le timeout de reset est écoulé → passe en half-open
            import time
            if self._open_since and (time.monotonic() - self._open_since) >= self._reset_timeout:
                self._state = "half-open"
                log.info("circuit_breaker_half_open", service="ollama")
                return False
            return True
        # half-open : on laisse passer un essai
        return False

    def _record_success(self):
        if self._state in ("half-open", "open"):
            log.info(
                "circuit_breaker_closed",
                service="ollama",
                previous_state=self._state,
            )
        self._fail_count = 0
        self._state      = "closed"
        self._open_since = None

    def _record_failure(self, exc: Exception):
        self._fail_count += 1
        log.error(
            "circuit_breaker_failure",
            service="ollama",
            fail_count=self._fail_count,
            fail_max=self._fail_max,
            error=str(exc),
        )
        if self._fail_count >= self._fail_max and self._state == "closed":
            import time
            self._state      = "open"
            self._open_since = time.monotonic()
            log.warning(
                "circuit_breaker_opened",
                service="ollama",
                fail_count=self._fail_count,
                reset_in_s=self._reset_timeout,
            )

    # ── Interface publique ────────────────────────────────────────────────────
    async def chat(self, payload: dict) -> dict:
        """
        Envoie un payload à /api/chat d'Ollama.

        Réessaie avec backoff exponentiel + jitter sur les erreurs réseau.
        Lève CircuitOpenError si le circuit est ouvert.

        Args:
            payload: dict conforme à l'API Ollama /api/chat

        Returns:
            dict: réponse JSON d'Ollama

        Raises:
            CircuitOpenError: si le circuit breaker est ouvert
            httpx.HTTPError: si toutes les tentatives échouent
        """
        if self._is_open():
            log.warning("circuit_open_request_blocked", service="ollama", state=self._state)
            raise CircuitOpenError("Circuit breaker ouvert — Ollama considéré indisponible.")

        last_exc: Exception | None = None

        for attempt, delay in enumerate(self._BACKOFF_DELAYS):
            try:
                result = await self._do_chat(payload)
                self._record_success()
                if attempt > 0:
                    log.info("ollama_retry_success", attempt=attempt + 1)
                return result

            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.RemoteProtocolError, httpx.HTTPStatusError) as e:
                last_exc = e
                self._record_failure(e)

                # Si le circuit vient de s'ouvrir, on arrête immédiatement
                if self._state == "open":
                    raise CircuitOpenError(
                        f"Circuit ouvert après {self._fail_count} échecs."
                    ) from e

                # Jitter aléatoire ±25% pour éviter les tempêtes de retry
                jitter = delay * random.uniform(-0.25, 0.25)
                wait   = max(0.1, delay + jitter)

                log.warning(
                    "ollama_retry",
                    attempt=attempt + 1,
                    max_attempts=len(self._BACKOFF_DELAYS),
                    wait_s=round(wait, 2),
                    error=str(e),
                )

                # Pas de sleep après la dernière tentative
                if attempt < len(self._BACKOFF_DELAYS) - 1:
                    await asyncio.sleep(wait)

        # Toutes les tentatives épuisées
        log.error(
            "ollama_all_retries_exhausted",
            attempts=len(self._BACKOFF_DELAYS),
            error=str(last_exc),
        )
        raise last_exc

    async def _do_chat(self, payload: dict) -> dict:
        """Effectue l'appel HTTP réel (nouvelle connexion à chaque fois — pas de leak)."""
        async with httpx.AsyncClient(timeout=180.0) as c:
            resp = await c.post(f"{CFG.OLLAMA}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()


# ─── Singleton ────────────────────────────────────────────────────────────────
_ollama_client: OllamaClient | None = None


def get_ollama_client() -> OllamaClient:
    """Retourne le singleton OllamaClient (création lazy)."""
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = OllamaClient()
    return _ollama_client
