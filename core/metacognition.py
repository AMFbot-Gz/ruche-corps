"""
core/metacognition.py — Réflexion autonome nocturne

Chaque nuit à 3h00 :
1. Analyser toutes les missions de la journée (goals.db + plans.jsonl)
2. Identifier les patterns de succès et d'échec
3. Généraliser en règles (via Nemotron)
4. Stocker dans mémoire sémantique (memory.py store_semantic)
5. Ajuster les priorités des objectifs futurs
6. Générer un rapport matinal pour le briefing 8h

Peut aussi être déclenché manuellement.
"""

import asyncio
import json
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import httpx

from config import CFG
from core.logger import get_logger

log = get_logger("metacognition")

PLANS_FILE       = Path.home() / ".ruche" / "plans.jsonl"
REFLECT_REPORT_DIR = Path.home() / ".ruche" / "reports" / "reflections"

REFLECTION_PROMPT = """\
Tu es un agent IA qui réfléchit sur ses propres performances.

MISSIONS D'AUJOURD'HUI:
{missions_summary}

STATISTIQUES:
- Réussies: {done} / {total}
- Taux de succès: {rate:.0%}
- Outils les plus utilisés: {top_tools}
- Erreurs les plus fréquentes: {top_errors}

Analyse en JSON:
{{
  "patterns_succes": ["pattern1", "pattern2"],
  "patterns_echec": ["pattern1", "pattern2"],
  "regles_generalisees": [
    {{"regle": "quand X, faire Y plutôt que Z", "confiance": 0.8}}
  ],
  "lacunes_identifiees": ["lacune1", "lacune2"],
  "recommandations": ["reco1", "reco2"],
  "score_journee": 0.75
}}"""


class MetacognitionEngine:
    """
    Moteur de réflexion autonome.

    Méthodes:
        async reflect() → dict : lance une session de réflexion complète
        async _load_today_missions() → list : charge les missions du jour depuis plans.jsonl
        async _analyze(missions) → dict : appelle Nemotron pour analyser
        async _store_insights(analysis) → int : stocke les règles en mémoire sémantique
        async _generate_report(analysis) → str : rapport texte pour le briefing
        async schedule() : boucle asyncio qui attend 3h00 chaque nuit
        async reflect_now() -> str : déclencher manuellement (retourne rapport)
    """

    def __init__(self):
        self._last_reflection: Optional[date] = None

    # ─── Boucle nocturne ──────────────────────────────────────────────────────

    async def schedule(self):
        """Boucle qui se déclenche chaque nuit à 3h00."""
        log.info("metacognition_scheduled")
        while True:
            now    = datetime.now()
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            log.info("next_reflection", in_hours=round(wait_sec / 3600, 1))
            await asyncio.sleep(wait_sec)

            if self._last_reflection != date.today():
                try:
                    await self.reflect()
                    self._last_reflection = date.today()
                except Exception as e:
                    log.error("reflection_failed", error=str(e))

    # ─── Session de réflexion ─────────────────────────────────────────────────

    async def reflect(self) -> dict:
        """Session de réflexion complète."""
        log.info("reflection_started")
        missions = await self._load_today_missions()
        if not missions:
            log.info("no_missions_to_reflect")
            return {}

        analysis     = await self._analyze(missions)
        rules_stored = await self._store_insights(analysis)
        report       = await self._generate_report(analysis)

        # Sauvegarder le rapport
        REFLECT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_file = REFLECT_REPORT_DIR / f"reflection_{date.today().isoformat()}.md"
        report_file.write_text(report)

        log.info(
            "reflection_done",
            missions=len(missions),
            rules_stored=rules_stored,
            score=analysis.get("score_journee", 0),
        )
        return analysis

    async def reflect_now(self) -> str:
        """Déclencher une réflexion manuelle. Retourne le rapport."""
        analysis = await self.reflect()
        if not analysis:
            return "Aucune mission à analyser aujourd'hui."
        return await self._generate_report(analysis)

    # ─── Chargement des missions du jour ──────────────────────────────────────

    async def _load_today_missions(self) -> list:
        """Charge les missions exécutées aujourd'hui depuis plans.jsonl."""
        if not PLANS_FILE.exists():
            return []

        today     = date.today().isoformat()
        missions  = []

        try:
            for line in PLANS_FILE.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    plan = json.loads(line)
                except Exception:
                    continue
                # Garder seulement les plans du jour
                started = plan.get("started_at", "") or plan.get("created_at", "")
                if today in started:
                    missions.append(plan)
        except Exception as e:
            log.error("load_missions_failed", error=str(e))

        # Compléter avec goals.db si disponible
        missions += self._load_goals_db_today(today)
        return missions

    def _load_goals_db_today(self, today: str) -> list:
        """Charge les objectifs terminés aujourd'hui depuis goals.db."""
        import sqlite3
        db_path = Path.home() / ".ruche" / "goals.db"
        if not db_path.exists():
            return []
        try:
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    "SELECT id, description, status, result, error, learned "
                    "FROM goals WHERE executed_at LIKE ? AND status IN ('done','failed')",
                    (f"{today}%",),
                ).fetchall()
            return [
                {
                    "id":          r[0],
                    "goal":        r[1],
                    "status":      r[2],
                    "result":      r[3] or "",
                    "error":       r[4] or "",
                    "learned":     r[5] or "",
                    "_source":     "goals_db",
                }
                for r in rows
            ]
        except Exception as e:
            log.error("load_goals_db_failed", error=str(e))
            return []

    # ─── Analyse via Nemotron ─────────────────────────────────────────────────

    async def _analyze(self, missions: list) -> dict:
        """Appelle Nemotron pour analyser les missions et extraire des règles."""
        total = len(missions)
        done  = sum(
            1 for m in missions
            if m.get("status") in ("done", "completed")
            or (isinstance(m.get("tasks"), list)
                and all(t.get("status") == "done" for t in m["tasks"]))
        )
        rate = done / total if total > 0 else 0.0

        # Extraire les outils utilisés et les erreurs
        all_tools  = []
        all_errors = []
        for m in missions:
            for task in m.get("tasks", []):
                if task.get("tool_hint"):
                    all_tools.append(task["tool_hint"])
                if task.get("error"):
                    all_errors.append(task["error"][:80])

        top_tools  = ", ".join(k for k, _ in Counter(all_tools).most_common(5)) or "aucun"
        top_errors = ", ".join(k for k, _ in Counter(all_errors).most_common(3)) or "aucune"

        # Construire le résumé des missions
        summary_lines = []
        for m in missions[:20]:  # Limiter à 20 pour le contexte
            goal   = m.get("goal") or m.get("description") or m.get("mission", "?")
            status = m.get("status", "?")
            result = (m.get("result") or "")[:100]
            summary_lines.append(f"- [{status}] {goal[:80]} → {result}")
        missions_summary = "\n".join(summary_lines)

        prompt = REFLECTION_PROMPT.format(
            missions_summary=missions_summary,
            done=done,
            total=total,
            rate=rate,
            top_tools=top_tools,
            top_errors=top_errors,
        )

        try:
            async with httpx.AsyncClient(timeout=120.0) as c:
                resp = await c.post(
                    f"{CFG.OLLAMA}/api/chat",
                    json={
                        "model":    CFG.M_GENERAL,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream":   False,
                        "options":  {
                            "temperature": 0.4,
                            "num_predict": 1200,
                            "num_ctx":     CFG.NEMOTRON_CTX,
                        },
                    },
                )
            raw = resp.json().get("message", {}).get("content", "{}")

            # Extraire le JSON de la réponse (peut contenir du texte autour)
            import re
            m = re.search(r'\{[\s\S]*\}', raw)
            analysis = json.loads(m.group()) if m else {}
        except Exception as e:
            log.error("analysis_llm_failed", error=str(e))
            # Analyse dégradée sans LLM
            analysis = {
                "patterns_succes":       [],
                "patterns_echec":        [],
                "regles_generalisees":   [],
                "lacunes_identifiees":   [],
                "recommandations":       [],
                "score_journee":         rate,
            }

        # Enrichir avec les stats calculées localement
        analysis["_stats"] = {
            "total":      total,
            "done":       done,
            "rate":       rate,
            "top_tools":  top_tools,
            "top_errors": top_errors,
        }
        return analysis

    # ─── Stockage des insights en mémoire sémantique ──────────────────────────

    async def _store_insights(self, analysis: dict) -> int:
        """Stocke les règles généralisées en mémoire sémantique. Retourne le nombre stocké."""
        regles = analysis.get("regles_generalisees", [])
        if not regles:
            return 0

        stored = 0
        try:
            from memory import RucheMemory
            mem = RucheMemory()
            await mem.initialize()
            try:
                for item in regles:
                    if not isinstance(item, dict):
                        continue
                    regle      = item.get("regle", "").strip()
                    confiance  = float(item.get("confiance", 0.7))
                    if not regle:
                        continue
                    await mem.store_semantic(
                        fact=regle,
                        source="metacognition",
                        confidence=confiance,
                    )
                    stored += 1
            finally:
                await mem.close()
        except Exception as e:
            log.error("store_insights_failed", error=str(e))

        return stored

    # ─── Génération du rapport ────────────────────────────────────────────────

    async def _generate_report(self, analysis: dict) -> str:
        """Génère un rapport Markdown lisible pour le briefing matinal."""
        stats = analysis.get("_stats", {})
        total = stats.get("total", 0)
        done  = stats.get("done", 0)
        rate  = stats.get("rate", 0.0)
        score = analysis.get("score_journee", rate)

        lines = [
            f"# Rapport de réflexion — {date.today().isoformat()}",
            "",
            f"## Résumé",
            f"- Missions analysées : {total}",
            f"- Réussies : {done} ({rate:.0%})",
            f"- Score journée : {score:.2f} / 1.00",
            "",
        ]

        patterns_ok  = analysis.get("patterns_succes", [])
        patterns_nok = analysis.get("patterns_echec", [])
        regles       = analysis.get("regles_generalisees", [])
        lacunes      = analysis.get("lacunes_identifiees", [])
        recos        = analysis.get("recommandations", [])

        if patterns_ok:
            lines += ["## Patterns de succès"] + [f"- {p}" for p in patterns_ok] + [""]

        if patterns_nok:
            lines += ["## Patterns d'échec"] + [f"- {p}" for p in patterns_nok] + [""]

        if regles:
            lines.append("## Règles généralisées")
            for r in regles:
                if isinstance(r, dict):
                    conf = r.get("confiance", 0.0)
                    lines.append(f"- [{conf:.0%}] {r.get('regle', r)}")
                else:
                    lines.append(f"- {r}")
            lines.append("")

        if lacunes:
            lines += ["## Lacunes identifiées"] + [f"- {l}" for l in lacunes] + [""]

        if recos:
            lines += ["## Recommandations"] + [f"- {r}" for r in recos] + [""]

        lines += [
            "---",
            f"*Généré automatiquement par MetacognitionEngine le {datetime.now().isoformat()}*",
        ]

        return "\n".join(lines)


# ─── Singleton ────────────────────────────────────────────────────────────────

_meta: Optional[MetacognitionEngine] = None


def get_metacognition() -> MetacognitionEngine:
    global _meta
    if _meta is None:
        _meta = MetacognitionEngine()
    return _meta
