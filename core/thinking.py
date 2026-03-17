"""
core/thinking.py — Pass de raisonnement caché avant chaque action

Avant de répondre, Nemotron fait une passe de réflexion interne:
- Qu'est-ce qu'on demande vraiment ?
- Qu'est-ce que je sais sur ce sujet ? (mémoire)
- Quels sont les risques ?
- Quel est mon plan ?
- Comment je vérifie le succès ?

Ces pensées sont stockées en mémoire procédurale et informent les futures décisions.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import json
import httpx
from config import CFG
from core.logger import get_logger

log = get_logger("thinking")

@dataclass
class Thought:
    intent: str          # Ce que l'utilisateur veut vraiment
    context_summary: str # Ce que l'agent sait de pertinent
    risks: list[str]     # Risques identifiés
    plan: list[str]      # Plan en étapes
    verification: str    # Comment vérifier le succès
    confidence: float    # 0.0 à 1.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_system_injection(self) -> str:
        """Retourne le thought comme texte à injecter dans le system prompt."""
        lines = [
            "=== ANALYSE INTERNE ===",
            f"Intention détectée: {self.intent}",
            f"Confiance: {self.confidence:.0%}",
        ]
        if self.risks:
            lines.append(f"Risques: {', '.join(self.risks)}")
        if self.plan:
            lines.append("Plan:")
            for i, step in enumerate(self.plan, 1):
                lines.append(f"  {i}. {step}")
        lines.append(f"Vérification: {self.verification}")
        lines.append("=== FIN ANALYSE ===")
        return "\n".join(lines)


THINK_PROMPT = """\
Analyse cette requête en 5 points. Réponds UNIQUEMENT en JSON valide, sans markdown.

REQUÊTE: {text}

CONTEXTE RÉCENT: {context}

JSON à retourner:
{{
  "intent": "ce que l'utilisateur veut vraiment accomplir (1 phrase précise)",
  "context_summary": "ce que tu sais de pertinent sur ce sujet (1-2 phrases)",
  "risks": ["risque1", "risque2"],
  "plan": ["étape1", "étape2", "étape3"],
  "verification": "comment vérifier que c'est réussi (critère objectif)",
  "confidence": 0.85
}}"""


class ThinkingLayer:
    """
    Effectue un pass de raisonnement silencieux avant chaque réponse.
    Les pensées sont stockées pour enrichir les futures décisions.
    """

    def __init__(self):
        self._cache: dict[str, Thought] = {}  # hash(text) → Thought

    async def think(self, text: str, context: str = "") -> Thought:
        """
        Génère une Thought pour la requête.
        Appel rapide (M_FAST, num_predict=400) pour ne pas ralentir la réponse.
        Retourne une Thought même en cas d'erreur (dégradée).
        """
        cache_key = str(hash(text[:200]))
        if cache_key in self._cache:
            return self._cache[cache_key]

        prompt = THINK_PROMPT.format(
            text=text[:500],
            context=context[:300] if context else "Aucun contexte récent."
        )

        model = getattr(CFG, 'M_FAST', CFG.M_GENERAL)
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.post(f"{CFG.OLLAMA}/api/chat", json={
                    "model": model,  # modèle rapide pour la pensée
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 400},
                })
            raw = resp.json().get("message", {}).get("content", "{}")
            import re
            m = re.search(r'\{[\s\S]*\}', raw)
            data = json.loads(m.group()) if m else {}
        except Exception as e:
            log.warning("think_failed", error=str(e))
            data = {}

        thought = Thought(
            intent=data.get("intent", text[:100]),
            context_summary=data.get("context_summary", ""),
            risks=data.get("risks", []),
            plan=data.get("plan", []),
            verification=data.get("verification", ""),
            confidence=float(data.get("confidence", 0.5)),
        )

        if len(self._cache) >= 200:
            # Supprimer la moitié la plus ancienne (LRU simple par ordre d'insertion)
            keys = list(self._cache.keys())
            for k in keys[:100]:
                del self._cache[k]
        self._cache[cache_key] = thought
        log.info("thought_generated",
                 intent=thought.intent[:60],
                 confidence=thought.confidence,
                 risks=len(thought.risks),
                 plan_steps=len(thought.plan))
        return thought

    def should_ask_confirmation(self, thought: Thought, autonomy_level: int = 3) -> bool:
        """
        Retourne True si l'agent devrait demander confirmation avant d'agir.
        Basé sur la confiance et le niveau d'autonomie configuré.
        """
        if autonomy_level >= 4:
            return False
        if autonomy_level <= 2:
            return True
        # Niveau 3 (défaut) : demander si confiance < 60% ou risques élevés
        return thought.confidence < 0.6 or len(thought.risks) > 2


# Singleton
_thinking = None
def get_thinking_layer() -> ThinkingLayer:
    global _thinking
    if _thinking is None:
        _thinking = ThinkingLayer()
    return _thinking
