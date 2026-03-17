"""
context/builder.py — Constructeur de contexte 1M tokens pour Nemotron-3-Super

Stratégie Claude :
  - Charge les fichiers pertinents dans le contexte
  - Encode intelligemment pour maximiser l'utilité par token
  - Respecte les limites (CFG.NEMOTRON_CTX = 128K tokens locaux)
  - Prioritise : mémoire récente > fichiers actifs > documentation

Usage :
    ctx = ContextBuilder()
    system = ctx.build_system(query="analyse ce code", files=["main.py", "agent.py"])
"""
import os
from pathlib import Path
from typing import Optional

from config import CFG, RUCHE_DIR

# ~3.5 chars par token en moyenne (French/English mix)
CHARS_PER_TOKEN  = 3.5
MAX_TOKENS       = CFG.NEMOTRON_CTX
# Réserver 10K tokens pour la réponse + historique
CONTEXT_BUDGET   = int((MAX_TOKENS - 10_000) * CHARS_PER_TOKEN)


# ─── Extensions à inclure automatiquement ────────────────────
_CODE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".sh", ".yaml", ".yml",
              ".json", ".toml", ".md", ".txt", ".env.example", ".cfg", ".ini"}

# ─── Répertoires à ignorer ────────────────────────────────────
_IGNORE_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
                "dist", "build", ".next", "chroma"}


class ContextBuilder:
    def __init__(self):
        self._budget_used = 0

    def _fits(self, text: str) -> bool:
        return (self._budget_used + len(text)) < CONTEXT_BUDGET

    def _add(self, text: str) -> str:
        """Ajoute du texte si le budget le permet, troncature sinon."""
        available = CONTEXT_BUDGET - self._budget_used
        if available <= 0:
            return ""
        chunk = text[:available]
        self._budget_used += len(chunk)
        return chunk

    # ─── Chargement de fichiers ───────────────────────────────
    def load_file(self, path: str | Path) -> str:
        """Charge un fichier texte avec header."""
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return ""
        if p.suffix not in _CODE_EXTS and p.stat().st_size > 50_000:
            return ""  # fichier binaire ou trop gros sans extension connue
        try:
            content = p.read_text(errors="replace")
            header  = f"\n{'='*60}\n📄 {p} ({len(content)} chars)\n{'='*60}\n"
            return self._add(header + content + "\n")
        except Exception:
            return ""

    def load_project(self, root: str | Path, max_files: int = 40) -> str:
        """Charge récursivement les fichiers d'un projet."""
        root = Path(root).expanduser()
        if not root.exists():
            return f"[Répertoire introuvable: {root}]\n"
        result = f"\n{'#'*60}\n# PROJET: {root}\n{'#'*60}\n"
        count  = 0
        # Trier par taille décroissante (les plus petits d'abord = plus dense en info)
        files  = sorted(
            (f for f in root.rglob("*")
             if f.is_file()
             and f.suffix in _CODE_EXTS
             and not any(p in _IGNORE_DIRS for p in f.parts)),
            key=lambda f: f.stat().st_size
        )
        for f in files:
            if count >= max_files:
                result += f"\n[...{len(files)-max_files} fichiers supplémentaires non inclus]\n"
                break
            chunk = self.load_file(f)
            if chunk:
                result += chunk
                count += 1
        return result

    def load_files(self, paths: list[str]) -> str:
        """Charge une liste de fichiers spécifiques."""
        result = ""
        for p in paths:
            result += self.load_file(p)
        return result

    def load_knowledge(self, facts: list[str]) -> str:
        """Injecte des faits/connaissances directement."""
        if not facts:
            return ""
        text = "\n📚 CONNAISSANCES CONTEXTUELLES:\n" + "\n".join(f"• {f}" for f in facts)
        return self._add(text + "\n")

    # ─── Builder principal ────────────────────────────────────
    def build(
        self,
        query:    str               = "",
        files:    Optional[list]    = None,
        projects: Optional[list]    = None,
        facts:    Optional[list]    = None,
        memory:   str               = "",
    ) -> str:
        """
        Construit le bloc de contexte à injecter dans le system prompt.
        Priorise : mémoire > fichiers spécifiques > projets > faits
        """
        self._budget_used = 0
        parts = []

        # 1. Mémoire récente (priorité max)
        if memory:
            parts.append(self._add(f"\n📡 MÉMOIRE PERTINENTE:\n{memory}\n"))

        # 2. Fichiers spécifiques demandés
        if files:
            parts.append(self.load_files(files))

        # 3. Projets entiers
        if projects:
            for proj in projects:
                parts.append(self.load_project(proj))

        # 4. Faits/connaissances
        if facts:
            parts.append(self.load_knowledge(facts))

        used_tokens = int(self._budget_used / CHARS_PER_TOKEN)
        budget_info = f"\n[Contexte: {used_tokens:,}/{MAX_TOKENS:,} tokens]\n"
        return budget_info + "".join(p for p in parts if p)

    # ─── Auto-détection des fichiers pertinents ───────────────
    def auto_files_for_query(self, query: str, project_root: str = None) -> list[str]:
        """
        Heuristique : identifie les fichiers pertinents pour une requête.
        Utile pour charger automatiquement le bon contexte.
        """
        root = Path(project_root or os.getcwd())
        candidates = []

        # Mots-clés dans la requête → fichiers correspondants
        q_lower = query.lower()
        keywords = {
            "config": ["config.py", "*.yml", "*.yaml", "*.toml", ".env"],
            "agent":  ["agent.py", "agent/core.py", "main.py"],
            "tool":   ["tools/builtins.py", "tools/registry.py"],
            "memory": ["memory.py", "agent/memory.py"],
            "telegram": ["senses/telegram.py"],
            "voice":  ["senses/voice.py"],
            "computer": ["computer/input.py", "computer/screen.py", "computer/sandbox.py"],
        }
        for kw, patterns in keywords.items():
            if kw in q_lower:
                for pat in patterns:
                    f = root / pat
                    if f.exists():
                        candidates.append(str(f))

        # Toujours inclure config + main si trop peu de fichiers
        if len(candidates) < 2:
            for f in ["config.py", "main.py", "agent.py"]:
                p = root / f
                if p.exists():
                    candidates.append(str(p))

        return list(dict.fromkeys(candidates))  # dédupliquer en gardant l'ordre
