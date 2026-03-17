"""
router.py — Routeur neuronal de La Ruche
Utilise llama3.2:3b pour classifier l'intent en < 100ms
et choisir le meilleur modèle pour chaque requête.

Amélioration vs PicoClaw :
  PicoClaw = heuristique (score de complexité basique)
  Ruche    = vrai LLM de classification + spécialistes par domaine
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from config import CFG as CONFIG
from core.logger import get_logger
from core.resilience import get_ollama_client, CircuitOpenError

log = get_logger(__name__)


# ─── Types de raisonnement ────────────────────────────────────────────────────
REASON_FAST      = "fast"       # < 1s  — question simple, heure, date
REASON_GENERAL   = "general"    # 3-8s  — conversation, analyse
REASON_CODE      = "code"       # 5-30s — écriture / debug de code
REASON_VISION    = "vision"     # 3-8s  — analyse d'image / écran
REASON_REASONING = "reasoning"  # 10-30s— problème complexe, math, plan
REASON_CREATIVE  = "creative"   # 5-15s — écriture, brainstorm

@dataclass
class RouteDecision:
    model:          str
    reasoning_type: str
    priority:       int   # 1=low … 5=urgent
    fast_path:      bool  # True = pas besoin de router (regex match)
    explanation:    str   = ""
    latency_ms:     float = 0.0


# ─── Règles rapides (pas de LLM, < 1ms) ──────────────────────────────────────
_FAST_RULES = [
    # (pattern regex, reasoning_type)
    (r"(quelle|il est|heure|date|jour|mois|année|time|clock)",        REASON_FAST),
    (r"(bonjour|salut|hello|hi\b|hey\b|merci|bonne nuit)",            REASON_FAST),
    (r"(stop|silence|arrête|quit|quitte)",                             REASON_FAST),
    (r"(screenshot|capture écran|montre.+écran)",                     REASON_VISION),
    (r"(qu.est.ce que tu vois|décris.+écran|analyse.+image)",         REASON_VISION),
    (r"(```|def |class |import |function|bug|erreur|error|syntax|code)", REASON_CODE),
    (r"(écris.+code|programme|implémente|refactorise|debug)",          REASON_CODE),
    (r"(pourquoi|comment|explique|analyse|réfléchi|stratégie|plan)",   REASON_REASONING),
]

_COMPILED_FAST_RULES = [(re.compile(p, re.IGNORECASE), t) for p, t in _FAST_RULES]


# ─── Mapping type → modèle ────────────────────────────────────────────────────
def _type_to_model(reasoning_type: str) -> str:
    return {
        REASON_FAST:      CONFIG.M_FAST,
        REASON_GENERAL:   CONFIG.M_GENERAL,
        REASON_CODE:      CONFIG.M_CODE,
        REASON_VISION:    CONFIG.M_VISION,
        REASON_REASONING: CONFIG.M_REASON,
        REASON_CREATIVE:  CONFIG.M_GENERAL,
    }.get(reasoning_type, CONFIG.M_GENERAL)


# ─── Prompt de classification ─────────────────────────────────────────────────
_ROUTER_PROMPT = """Tu es un classificateur d'intent ultra-rapide.
Analyse le message et réponds en JSON avec ces champs :
{
  "type": "fast|general|code|vision|reasoning|creative",
  "priority": 1-5,
  "reason": "explication courte"
}

Règles :
- fast       : questions simples (heure, météo, oui/non)
- general    : conversation, résumés, explications courtes
- code       : écrire/analyser/debugger du code
- vision     : analyser une image, un écran, des captures
- reasoning  : problèmes complexes, math, planification stratégique
- creative   : écriture, idées, brainstorm
- priority 5 : urgent (erreur critique, sécurité, panne)
- priority 1 : info passive, pas besoin de réponse rapide

Réponds UNIQUEMENT avec le JSON, rien d'autre."""


# ─── Router ───────────────────────────────────────────────────────────────────
class Router:
    def __init__(self, config=CONFIG):
        self.cfg     = config
        self._client = httpx.AsyncClient(timeout=5.0)

    async def classify(self, text: str, history_len: int = 0) -> RouteDecision:
        t0 = time.monotonic()

        # 1. Chemin ultra-rapide : règles regex
        for pattern, r_type in _COMPILED_FAST_RULES:
            if pattern.search(text):
                return RouteDecision(
                    model=_type_to_model(r_type),
                    reasoning_type=r_type,
                    priority=3 if r_type != REASON_FAST else 2,
                    fast_path=True,
                    explanation=f"regex:{r_type}",
                    latency_ms=(time.monotonic() - t0) * 1000,
                )

        # 2. Chemin LLM rapide : llama3.2:3b
        try:
            resp = await self._client.post(
                f"{CONFIG.OLLAMA}/api/chat",
                json={
                    "model": CONFIG.M_ROUTER,
                    "messages": [
                        {"role": "system", "content": _ROUTER_PROMPT},
                        {"role": "user",   "content": text[:500]},  # tronqué pour vitesse
                    ],
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 80},
                },
            )
            raw = resp.json().get("message", {}).get("content", "{}")
            # Extraire JSON même si le modèle ajoute du texte
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    data       = json.loads(m.group())
                    r_type     = data.get("type", REASON_GENERAL)
                    priority   = int(data.get("priority", 3))
                    explanation = data.get("reason", "")
                    return RouteDecision(
                        model=_type_to_model(r_type),
                        reasoning_type=r_type,
                        priority=priority,
                        fast_path=False,
                        explanation=explanation,
                        latency_ms=(time.monotonic() - t0) * 1000,
                    )
                except (json.JSONDecodeError, ValueError) as parse_err:
                    log.error("router_json_parse_error",
                              raw=raw[:200],
                              error=str(parse_err))
                    # Fallback ci-dessous
        except CircuitOpenError as e:
            log.warning("router_circuit_open", error=str(e))
        except Exception as e:
            log.warning("router_llm_error", error=str(e))

        # 3. Fallback : longueur du texte comme heuristique
        r_type = REASON_GENERAL if len(text) < 200 else REASON_REASONING
        log.debug("router_fallback", text_len=len(text), chosen_type=r_type)
        return RouteDecision(
            model=CONFIG.M_GENERAL,
            reasoning_type=r_type,
            priority=3,
            fast_path=False,
            explanation="fallback:length",
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    async def close(self):
        await self._client.aclose()
