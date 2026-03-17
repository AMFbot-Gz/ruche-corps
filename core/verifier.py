"""
core/verifier.py — Vérification objective des résultats d'outils

Chaque type d'action a une post-condition vérifiable.
On ne fait PAS confiance au LLM pour dire "c'est bon".
On vérifie objectivement.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
import asyncio
import json


@dataclass
class VerificationResult:
    success: bool
    message: str
    evidence: str = ""  # Ce qui prouve le succès/échec


class Verifier:
    """
    Vérifie objectivement le résultat d'une action.

    Usage:
        v = Verifier()
        result = await v.verify("write_file",
                                params={"path": "/tmp/test.txt"},
                                llm_result="Fichier créé avec succès")
    """

    # Mapping: préfixes/noms d'outils → méthode de vérification
    _TOOL_MAP: dict[str, str] = {
        "write_file":   "_verify_write_file",
        "edit_file":    "_verify_edit_file",
        "shell":        "_verify_shell",
        "web_search":   "_verify_web_search",
        "run_python":   "_verify_shell",
    }

    async def verify(self, tool_name: str, params: dict, llm_result: str) -> VerificationResult:
        """
        Vérifie le résultat d'un appel d'outil.
        Si pas de vérificateur: retourne success=True (bénéfice du doute).
        """
        method_name = self._TOOL_MAP.get(tool_name)
        if method_name is None:
            return VerificationResult(True, "Non vérifié (bénéfice du doute)")

        method = getattr(self, method_name, None)
        if method is None:
            return VerificationResult(True, "Non vérifié (bénéfice du doute)")

        try:
            return await method(params, llm_result)
        except Exception as e:
            return VerificationResult(False, f"Erreur lors de la vérification: {e}")

    # ─── Vérificateurs spécifiques ────────────────────────────────────────────

    async def _verify_write_file(self, params: dict, result: str) -> VerificationResult:
        """Vérifie que le fichier existe et n'est pas vide."""
        path = Path(params.get("path", "")).expanduser()
        if not str(path):
            return VerificationResult(False, "Chemin de fichier manquant dans les paramètres")
        if path.exists() and path.stat().st_size > 0:
            return VerificationResult(True, "Fichier créé", f"{path.stat().st_size} bytes")
        if path.exists():
            return VerificationResult(False, f"Fichier vide: {path}")
        return VerificationResult(False, f"Fichier absent: {path}")

    async def _verify_shell(self, params: dict, result: str) -> VerificationResult:
        """Vérifie que le returncode était 0 (parsé depuis le résultat)."""
        if "ERREUR" in result or "BLOQUÉ" in result:
            return VerificationResult(False, "Shell a retourné une erreur", result[:200])
        lowered = result.lower()
        if "error" in lowered or "failed" in lowered or "traceback" in lowered:
            return VerificationResult(False, "Shell a retourné une erreur", result[:200])
        return VerificationResult(True, "Shell OK", result[:100])

    async def _verify_web_search(self, params: dict, result: str) -> VerificationResult:
        """Vérifie qu'on a au moins un résultat."""
        if len(result) > 50 and "Aucun" not in result and "Erreur" not in result:
            return VerificationResult(True, "Résultats trouvés", f"{len(result)} chars")
        return VerificationResult(False, "Recherche vide ou en erreur")

    async def _verify_edit_file(self, params: dict, result: str) -> VerificationResult:
        """Vérifie que le fichier existe après édition."""
        path = Path(params.get("path", "")).expanduser()
        if not str(path):
            return VerificationResult(False, "Chemin de fichier manquant dans les paramètres")
        if path.exists():
            return VerificationResult(True, "Fichier modifié", str(path))
        return VerificationResult(False, f"Fichier introuvable: {path}")


# ─── Singleton ────────────────────────────────────────────────────────────────

_verifier: Optional[Verifier] = None


def get_verifier() -> Verifier:
    global _verifier
    if _verifier is None:
        _verifier = Verifier()
    return _verifier
