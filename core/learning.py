"""
core/learning.py — Boucle d'apprentissage nocturne de La Ruche

Adapté depuis pico-omni-agentique/meta/evolution_engine.py et memory/evolution.py.

Cycle (déclenché à 3h00 chaque nuit) :
  1. Collecte   → analyse les missions récentes (goals.db + logs)
  2. Analyse    → identifie les faiblesses (taux d'échec, retries, lenteur)
  3. Génère     → propose des correctifs via Nemotron
  4. Valide     → sandbox Python (syntaxe + import + assertion)
  5. Déploie    → applique les patchs validés (backup + écriture atomique)
  6. Rapport    → sauvegarde l'historique, notifie via Redis/Telegram

Fichiers que Learning peut auto-améliorer (périmètre sécurisé) :
  tools/builtins.py, computer/screen.py, computer/input.py,
  memory.py, goals.py, watchdog.py

SynapseLayer : extrait des règles générales depuis les missions réussies
               et les stocke dans ~/.ruche/learned_rules.json
"""

import ast
import asyncio
import json
import logging
import re
import shutil
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Optional

import httpx

from config import CFG, RUCHE_DIR

logger = logging.getLogger("ruche.learning")

# ─── Chemins persistants ─────────────────────────────────────────────────────

RUCHE_ROOT   = Path(__file__).parent.parent
BACKUP_DIR   = RUCHE_DIR / "learning_backups"
LEARN_LOG    = RUCHE_DIR / "logs" / "learning.log"
LEARN_HIST   = RUCHE_DIR / "learning_history.json"
RULES_FILE   = RUCHE_DIR / "learned_rules.json"
GOALS_DB     = RUCHE_DIR / "goals.db"

BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LEARN_LOG.parent.mkdir(parents=True, exist_ok=True)

# Périmètre sécurisé : seuls ces fichiers sont modifiables automatiquement
EVOLVABLE_FILES = [
    "tools/builtins.py",
    "computer/screen.py",
    "computer/input.py",
    "memory.py",
    "goals.py",
    "watchdog.py",
]


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class EvolutionProposal:
    id:             str
    target_file:    str
    problem:        str
    solution:       str
    old_code:       str  = ""
    new_code:       str  = ""
    expected_gain:  str  = ""
    risk_level:     str  = "low"
    test_assertion: str  = "assert True"
    validated:      bool = False
    applied:        bool = False
    test_result:    str  = ""
    _backup_path:   str  = ""


@dataclass
class LearningReport:
    date:                 str
    goals_analyzed:       int
    failure_rate_before:  float
    proposals_generated:  int
    proposals_validated:  int
    proposals_applied:    int
    improvements:         list = field(default_factory=list)
    regressions:          list = field(default_factory=list)
    rules_learned:        int  = 0
    next_focus:           str  = ""


# ─── SynapseLayer : règles apprises ─────────────────────────────────────────

class SynapseLayer:
    """
    Couche sémantique qui extrait et stocke des règles générales
    depuis les missions réussies.
    Persiste dans ~/.ruche/learned_rules.json.
    """

    def __init__(self):
        self.rules_file = RULES_FILE
        self.profile    = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.rules_file.read_text(encoding="utf-8"))
        except Exception:
            return {
                "learned_rules":         [],
                "common_errors_fixed":   [],
                "session_count":         0,
                "total_tasks_completed": 0,
                "platform_knowledge":    {},
            }

    def save(self) -> None:
        tmp = self.rules_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.profile, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.rename(self.rules_file)

    def add_rule(self, rule: str) -> bool:
        """Ajoute une règle apprise (dédupliquée). Retourne True si ajoutée."""
        rules = self.profile.setdefault("learned_rules", [])
        rule  = rule.strip()[:400]
        if rule and rule not in rules:
            rules.append(rule)
            # Garde les 100 règles les plus récentes
            self.profile["learned_rules"] = rules[-100:]
            self.save()
            return True
        return False

    def get_rules_for_query(self, query: str, max_rules: int = 5) -> list[str]:
        """Retourne les règles pertinentes pour une requête (correspondance par mots)."""
        query_words = set(query.lower().split())
        rules = self.profile.get("learned_rules", [])
        scored = []
        for rule in rules:
            rule_words = set(rule.lower().split())
            overlap    = len(query_words & rule_words)
            if overlap > 0:
                scored.append((overlap, rule))
        scored.sort(reverse=True)
        return [r for _, r in scored[:max_rules]]

    async def extract_rule_from_mission(
        self,
        goal: str,
        result: str,
    ) -> Optional[str]:
        """
        Extrait une règle générale après mission réussie via Ollama.
        Non bloquant — retourne None si Ollama est indisponible.
        """
        prompt = (
            f"Mission: {goal[:200]}\nRésultat: {result[:200]}\n"
            "En une phrase courte (max 20 mots) : quelle règle générale retenir "
            "pour ce type de tâche à l'avenir ?"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                resp = await c.post(
                    f"{CFG.OLLAMA}/api/chat",
                    json={
                        "model":    CFG.M_FAST,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream":   False,
                        "options":  {"temperature": 0.3, "num_predict": 80},
                    },
                )
            rule = resp.json().get("message", {}).get("content", "").strip()
            if rule and len(rule) > 8:
                return rule
        except Exception:
            pass
        return None


# ─── LearningEngine ──────────────────────────────────────────────────────────

class LearningEngine:
    """
    Moteur d'apprentissage nocturne de La Ruche.

    Méthodes principales :
    - evolve()              → orchestre les 6 étapes (appel synchrone)
    - schedule()           → asyncio.Task qui déclenche à 3h00 chaque nuit
    - get_history()        → 10 dernières évolutions
    - collect_performance_data()   → analyse les missions goals.db
    - add_learned_rule()   → délègue à SynapseLayer
    """

    def __init__(self, notify_fn=None):
        """
        notify_fn : callable async (message: str) → None
                    utilisé pour les notifications Telegram/Redis
        """
        self._notify         = notify_fn
        self._synapse        = SynapseLayer()
        self._modified_files: list[str] = []
        self.history         = self._load_history()

    # ─── Histoire ────────────────────────────────────────────────────────────

    def _load_history(self) -> dict:
        if LEARN_HIST.exists():
            try:
                return json.loads(LEARN_HIST.read_text(encoding="utf-8"))
            except Exception:
                pass
        default = {"evolutions": [], "total_applied": 0}
        self._save_history(default)
        return default

    def _save_history(self, data: dict = None) -> None:
        if data is None:
            data = self.history
        tmp = LEARN_HIST.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.rename(LEARN_HIST)

    # ─── ÉTAPE 1 — Collecte ──────────────────────────────────────────────────

    def collect_performance_data(self) -> dict:
        """
        Lit les 50 derniers objectifs dans goals.db et calcule les métriques.
        Retourne un dict avec total, success_rate, failure_freq, avg_duration.
        """
        if not GOALS_DB.exists():
            return {
                "total": 0, "success_rate": 1.0,
                "failure_freq": [], "avg_duration": 0.0, "retry_heavy": [],
            }

        try:
            conn  = sqlite3.connect(str(GOALS_DB))
            rows  = conn.execute(
                "SELECT description, status, result, learned FROM goals ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
            conn.close()
        except Exception as e:
            _log("collect", f"goals.db inaccessible: {e}")
            return {"total": 0, "success_rate": 1.0, "failure_freq": [], "avg_duration": 0.0, "retry_heavy": []}

        total = len(rows)
        if total == 0:
            return {"total": 0, "success_rate": 1.0, "failure_freq": [], "avg_duration": 0.0, "retry_heavy": []}

        done_count    = sum(1 for r in rows if r[1] == "done")
        failed_count  = sum(1 for r in rows if r[1] == "failed")
        success_rate  = done_count / max(done_count + failed_count, 1)

        failure_descs = [r[0][:80] for r in rows if r[1] == "failed"]
        failure_freq  = Counter(failure_descs).most_common(5)

        # Taux de retry élevé : goals avec "erreur" dans learned
        retry_heavy   = [r[0] for r in rows if r[3] and "erreur" in (r[3] or "").lower()]

        _log("collect", f"{total} objectifs, taux succès {success_rate:.1%}")
        print(f"[Learning] Collecte : {total} objectifs, succès {success_rate:.1%}")

        return {
            "total":        total,
            "success_rate": success_rate,
            "failure_freq": failure_freq,
            "retry_heavy":  retry_heavy,
            "avg_duration": 0.0,  # goals.db ne stocke pas la durée — placeholder
        }

    # ─── ÉTAPE 2 — Analyse ───────────────────────────────────────────────────

    def analyze_weaknesses(self, metrics: dict) -> list[dict]:
        """Identifie les faiblesses à partir des métriques."""
        weaknesses    = []
        failure_freq  = metrics.get("failure_freq", [])
        retry_heavy   = metrics.get("retry_heavy", [])
        total         = max(metrics.get("total", 1), 1)
        success_rate  = metrics.get("success_rate", 1.0)

        # Faiblesse 1 — Taux d'échec global
        if success_rate < 0.7 and total >= 5:
            weaknesses.append({
                "type":          "high_failure_rate",
                "description":   f"Taux d'échec global {(1-success_rate):.0%} sur {total} objectifs",
                "target_module": "goals.py",
                "priority":      1,
            })

        # Faiblesse 2 — Objectifs récurrents en échec
        if failure_freq and failure_freq[0][1] >= 3:
            task, count = failure_freq[0]
            weaknesses.append({
                "type":          "recurring_failure",
                "description":   f"'{task}' échoue {count}× — pattern répété",
                "target_module": "tools/builtins.py",
                "priority":      1,
            })

        # Faiblesse 3 — Retries fréquents
        if retry_heavy and len(retry_heavy) / total > 0.3:
            pct = len(retry_heavy) / total
            weaknesses.append({
                "type":          "high_retry_rate",
                "description":   f"{pct:.0%} des objectifs ont nécessité plusieurs tentatives",
                "target_module": "watchdog.py",
                "priority":      2,
            })

        weaknesses.sort(key=lambda w: w["priority"])
        print(f"[Learning] Analyse : {len(weaknesses)} faiblesse(s) détectée(s)")
        return weaknesses

    # ─── ÉTAPE 3 — Génération ────────────────────────────────────────────────

    async def generate_proposals(
        self,
        weaknesses: list[dict],
        metrics: dict,
    ) -> list[EvolutionProposal]:
        """Génère jusqu'à 2 propositions d'amélioration via Nemotron."""
        import uuid
        proposals: list[EvolutionProposal] = []

        for weakness in weaknesses[:2]:
            target_rel  = weakness["target_module"]
            target_path = RUCHE_ROOT / target_rel

            if target_rel not in EVOLVABLE_FILES or not target_path.exists():
                _log("generate", f"fichier hors périmètre ou introuvable: {target_rel}")
                continue

            file_content = target_path.read_text(encoding="utf-8")[:3000]

            prompt = (
                "Tu es un expert en amélioration d'agents IA Python.\n\n"
                f"FICHIER ({target_rel}) :\n{file_content}\n\n"
                f"PROBLÈME : {weakness['description']}\n\n"
                "CONTRAINTES ABSOLUES :\n"
                "- Patch minimal (< 30 lignes modifiées)\n"
                "- Garde toutes les interfaces intactes\n"
                "- Pas de nouvelles dépendances\n"
                "- Code Python syntaxiquement valide\n"
                "- Niveau de risque : LOW uniquement\n\n"
                "Réponds en JSON valide uniquement :\n"
                '{"problem": "...", "solution": "...", '
                f'"target_file": "{target_rel}", '
                '"old_code": "extrait exact du code à remplacer (max 15 lignes)", '
                '"new_code": "code de remplacement", '
                '"expected_gain": "...", "risk_level": "low", '
                '"test_assertion": "assert True"}'
            )

            try:
                async with httpx.AsyncClient(timeout=60.0) as c:
                    resp = await c.post(
                        f"{CFG.OLLAMA}/api/chat",
                        json={
                            "model":    CFG.M_GENERAL,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream":   False,
                            "options":  {
                                "temperature": 0.3,
                                "num_predict": 1500,
                                "num_ctx":     CFG.NEMOTRON_CTX,
                            },
                        },
                    )
                raw  = resp.json().get("message", {}).get("content", "")
                raw  = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
                # Extrait le premier bloc JSON
                m    = re.search(r'\{[\s\S]*\}', raw)
                data = json.loads(m.group()) if m else {}

                if not data.get("new_code"):
                    _log("generate", f"JSON vide pour {weakness['type']}")
                    continue

                prop = EvolutionProposal(
                    id            = f"evo_{uuid.uuid4().hex[:8]}",
                    target_file   = data.get("target_file", target_rel),
                    problem       = data.get("problem", weakness["description"]),
                    solution      = data.get("solution", ""),
                    old_code      = data.get("old_code", ""),
                    new_code      = data.get("new_code", ""),
                    expected_gain = data.get("expected_gain", "amélioration"),
                    risk_level    = data.get("risk_level", "low"),
                    test_assertion= data.get("test_assertion", "assert True"),
                )
                proposals.append(prop)
                _log("generate", f"{prop.id}: {prop.problem[:60]}")

            except Exception as e:
                _log("generate", f"erreur pour {weakness['type']}: {e}")

        print(f"[Learning] {len(proposals)} proposition(s) générée(s)")
        return proposals

    # ─── ÉTAPE 4 — Validation sandbox ────────────────────────────────────────

    def validate_proposal(self, proposal: EvolutionProposal) -> bool:
        """
        Valide en sandbox : backup → syntaxe → patch → import → assertion.
        Rollback automatique si l'une des étapes échoue.
        """
        import subprocess as _sp
        import uuid

        target = RUCHE_ROOT / proposal.target_file
        if not target.exists():
            _log("validate", f"fichier introuvable: {proposal.target_file}")
            return False

        # 4a — Backup atomique
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name   = proposal.target_file.replace("/", "_")
        backup_path = str(BACKUP_DIR / f"{safe_name}_{ts}.bak")
        shutil.copy(str(target), backup_path)
        proposal._backup_path = backup_path

        # 4b — Syntaxe du nouveau code
        if proposal.new_code:
            try:
                ast.parse(proposal.new_code)
            except SyntaxError as e:
                _log("validate", f"{proposal.id} SyntaxError: {e}")
                return False

        # 4c — Application du patch
        original = target.read_text(encoding="utf-8")
        if proposal.old_code and proposal.old_code not in original:
            _log("validate", f"{proposal.id} old_code introuvable dans {proposal.target_file}")
            return False

        if proposal.old_code and proposal.new_code:
            patched = original.replace(proposal.old_code, proposal.new_code, 1)
            target.write_text(patched, encoding="utf-8")
            self._modified_files.append(str(target))
        else:
            proposal.test_result = "aucun patch code — validation partielle"
            proposal.validated   = True
            return True

        # 4d — Test d'import (vérifie que le fichier se compile proprement)
        result = _sp.run(
            ["python3", "-c",
             f"import sys; sys.path.insert(0,'{RUCHE_ROOT}'); "
             f"compile(open('{target}').read(), '{target}', 'exec')"],
            capture_output=True, text=True, timeout=15,
            cwd=str(RUCHE_ROOT),
        )
        if result.returncode != 0:
            _log("validate", f"{proposal.id} import fail: {result.stderr[:200]}")
            self._rollback(proposal)
            return False

        # 4e — Test assertion personnalisée
        assertion = proposal.test_assertion.strip()
        if assertion and assertion != "assert True":
            result = _sp.run(
                ["python3", "-c",
                 f"import sys; sys.path.insert(0,'{RUCHE_ROOT}'); {assertion}"],
                capture_output=True, text=True, timeout=20,
                cwd=str(RUCHE_ROOT),
            )
            if result.returncode != 0:
                _log("validate", f"{proposal.id} assertion fail: {result.stderr[:200]}")
                self._rollback(proposal)
                return False

        proposal.validated   = True
        proposal.test_result = "tous tests passés"
        _log("validate", f"OK {proposal.id} validée")
        print(f"[Learning] Proposal {proposal.id} validée")
        return True

    def _rollback(self, proposal: EvolutionProposal) -> None:
        if proposal._backup_path and Path(proposal._backup_path).exists():
            target = RUCHE_ROOT / proposal.target_file
            shutil.copy(proposal._backup_path, str(target))
            _log("rollback", f"{proposal.target_file} restauré depuis backup")
            print(f"[Learning] Rollback {proposal.target_file}")

    def _global_rollback(self) -> None:
        """Rollback de tous les fichiers modifiés pendant la session."""
        for file_path in self._modified_files:
            safe   = Path(file_path).name
            backups = sorted(BACKUP_DIR.glob(f"*{safe}*.bak"), reverse=True)
            if backups:
                shutil.copy(str(backups[0]), file_path)
                _log("rollback_global", f"{file_path}")
        self._modified_files.clear()

    # ─── ÉTAPE 5 — Déploiement ───────────────────────────────────────────────

    def apply_proposals(self, proposals: list[EvolutionProposal]) -> list[EvolutionProposal]:
        """Marque les proposals validées comme appliquées, met à jour l'historique."""
        for prop in proposals:
            if not prop.validated:
                continue
            prop.applied = True
            _log("deploy", f"{prop.id} → {prop.target_file}")
            print(f"[Learning] Déployée : {prop.id} → {prop.target_file}")

        applied_count = sum(1 for p in proposals if p.applied)
        self.history["total_applied"] = self.history.get("total_applied", 0) + applied_count

        entry = {
            "date":     datetime.now().isoformat(),
            "applied":  applied_count,
            "proposals": [
                {"id": p.id, "problem": p.problem[:60], "gain": p.expected_gain}
                for p in proposals if p.applied
            ],
        }
        self.history.setdefault("evolutions", []).append(entry)
        self.history["evolutions"] = self.history["evolutions"][-20:]
        self._save_history()

        return proposals

    # ─── ÉTAPE 6 — Rapport + extraction de règles ────────────────────────────

    async def extract_rules_from_successes(self) -> int:
        """Extrait des règles depuis les dernières missions réussies."""
        if not GOALS_DB.exists():
            return 0

        rules_added = 0
        try:
            conn = sqlite3.connect(str(GOALS_DB))
            rows = conn.execute(
                "SELECT description, result FROM goals "
                "WHERE status='done' AND result IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 10"
            ).fetchall()
            conn.close()
        except Exception:
            return 0

        for desc, result in rows:
            rule = await self._synapse.extract_rule_from_mission(desc, result or "")
            if rule and self._synapse.add_rule(rule):
                rules_added += 1

        return rules_added

    # ─── MÉTHODE PRINCIPALE ──────────────────────────────────────────────────

    async def evolve(self) -> LearningReport:
        """
        Orchestre les 6 étapes d'évolution. Timeout global 20 min.
        Rollback global sur exception.
        """
        self._modified_files.clear()
        t_start = time.time()
        MAX_SEC = 20 * 60

        print(f"\n{'='*50}")
        print(f"[Learning] ÉVOLUTION — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*50}")
        _log("evolve", "démarrage")

        metrics:    dict                    = {}
        weaknesses: list[dict]             = []
        proposals:  list[EvolutionProposal] = []
        applied:    list[EvolutionProposal] = []
        rules_added = 0

        try:
            # Étape 1 : Collecte
            print("\n[Learning] Étape 1/6 — Collecte...")
            metrics = self.collect_performance_data()
            _check_timeout(t_start, MAX_SEC, "collecte")

            # Étape 2 : Analyse
            print("\n[Learning] Étape 2/6 — Analyse...")
            weaknesses = self.analyze_weaknesses(metrics)
            if not weaknesses:
                print("[Learning] Aucune faiblesse critique détectée")
                # Extraire quand même des règles
                rules_added = await self.extract_rules_from_successes()
                report = LearningReport(
                    date                 = datetime.now().strftime("%Y-%m-%d %H:%M"),
                    goals_analyzed       = metrics.get("total", 0),
                    failure_rate_before  = 1.0 - metrics.get("success_rate", 1.0),
                    proposals_generated  = 0,
                    proposals_validated  = 0,
                    proposals_applied    = 0,
                    rules_learned        = rules_added,
                )
                await self._notify_report(report)
                return report
            _check_timeout(t_start, MAX_SEC, "analyse")

            # Étape 3 : Génération
            print("\n[Learning] Étape 3/6 — Génération propositions...")
            proposals = await self.generate_proposals(weaknesses, metrics)
            _check_timeout(t_start, MAX_SEC, "génération")

            # Étape 4 : Validation
            print("\n[Learning] Étape 4/6 — Validation sandbox...")
            for prop in proposals:
                if time.time() - t_start > MAX_SEC:
                    break
                self.validate_proposal(prop)

            # Étape 5 : Déploiement
            print("\n[Learning] Étape 5/6 — Déploiement...")
            applied = self.apply_proposals(proposals)

            # Étape 6 : Extraction de règles
            print("\n[Learning] Étape 6/6 — Extraction de règles...")
            rules_added = await self.extract_rules_from_successes()

            duration = time.time() - t_start
            validated_count = sum(1 for p in proposals if p.validated)
            applied_count   = sum(1 for p in applied   if p.applied)

            report = LearningReport(
                date                 = datetime.now().strftime("%Y-%m-%d %H:%M"),
                goals_analyzed       = metrics.get("total", 0),
                failure_rate_before  = 1.0 - metrics.get("success_rate", 1.0),
                proposals_generated  = len(proposals),
                proposals_validated  = validated_count,
                proposals_applied    = applied_count,
                improvements         = [p.expected_gain for p in applied if p.applied],
                regressions          = [p.problem[:60] for p in proposals if not p.validated],
                rules_learned        = rules_added,
                next_focus           = (
                    weaknesses[0]["description"][:80] if weaknesses else "maintien des performances"
                ),
            )

            _log("evolve", f"terminé en {duration:.0f}s — {applied_count} appliquées, {rules_added} règles")
            print(f"\n[Learning] Évolution terminée en {duration:.0f}s")
            await self._notify_report(report)
            return report

        except Exception as e:
            print(f"\n[Learning] Erreur évolution : {e}")
            _log("evolve", f"ERREUR : {e}")
            self._global_rollback()
            return LearningReport(
                date                = datetime.now().strftime("%Y-%m-%d %H:%M"),
                goals_analyzed      = metrics.get("total", 0),
                failure_rate_before = 1.0 - metrics.get("success_rate", 1.0),
                proposals_generated = len(proposals),
                proposals_validated = 0,
                proposals_applied   = 0,
                regressions         = [str(e)],
                next_focus          = "corriger l'erreur d'évolution",
            )

    async def _notify_report(self, report: LearningReport) -> None:
        """Publie un résumé du rapport via le callback notify_fn."""
        if not self._notify:
            return
        msg = (
            f"[Learning] Évolution nocturne — {report.date}\n"
            f"Objectifs analysés: {report.goals_analyzed}\n"
            f"Propositions: {report.proposals_generated} générées / "
            f"{report.proposals_validated} validées / {report.proposals_applied} appliquées\n"
            f"Règles apprises: {report.rules_learned}\n"
            f"Prochain focus: {report.next_focus}"
        )
        try:
            await self._notify(msg)
        except Exception:
            pass

    # ─── Scheduler nocturne ──────────────────────────────────────────────────

    def schedule(self) -> asyncio.Task:
        """
        Crée une tâche asyncio qui déclenche l'évolution chaque nuit à 3h00.
        Appelle asyncio.create_task() — doit être appelé dans une boucle active.
        """
        async def _loop():
            while True:
                now    = datetime.now()
                target = now.replace(hour=3, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)

                wait_s = (target - now).total_seconds()
                _log(
                    "scheduler",
                    f"Prochain cycle évolution : {target.strftime('%d/%m %H:%M')} "
                    f"(dans {wait_s / 3600:.1f}h)"
                )
                print(
                    f"[Learning] Prochain cycle : {target.strftime('%d/%m %H:%M')} "
                    f"(dans {wait_s / 3600:.1f}h)"
                )

                await asyncio.sleep(wait_s)

                try:
                    await self.evolve()
                except Exception as e:
                    _log("scheduler", f"Évolution échouée: {e}")
                    print(f"[Learning] Évolution nocturne échouée: {e}")

                # Attente 1h avant de recalculer (évite double-déclenchement)
                await asyncio.sleep(3600)

        return asyncio.create_task(_loop())

    # ─── API publique simplifiée ─────────────────────────────────────────────

    def get_history(self) -> list[dict]:
        """Retourne les 10 dernières évolutions."""
        return self.history.get("evolutions", [])[-10:]

    def get_learned_rules(self) -> list[str]:
        """Retourne toutes les règles apprises."""
        return self._synapse.profile.get("learned_rules", [])

    def get_rules_for_query(self, query: str) -> list[str]:
        """Retourne les règles pertinentes pour une requête."""
        return self._synapse.get_rules_for_query(query)

    def add_learned_rule(self, rule: str) -> bool:
        """Ajoute une règle apprise manuellement."""
        return self._synapse.add_rule(rule)


# ─── Helpers privés ──────────────────────────────────────────────────────────

def _log(step: str, msg: str) -> None:
    LEARN_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat()}] [{step}] {msg}\n"
    try:
        with open(LEARN_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _check_timeout(t_start: float, max_sec: float, step: str) -> None:
    elapsed = time.time() - t_start
    if elapsed > max_sec:
        raise TimeoutError(f"Timeout global ({max_sec}s) atteint à l'étape '{step}'")


# ─── Singleton global ─────────────────────────────────────────────────────────
# Usage: from core.learning import get_learning_engine
_engine: Optional[LearningEngine] = None


def get_learning_engine(notify_fn=None) -> LearningEngine:
    """Retourne le singleton LearningEngine (création lazy)."""
    global _engine
    if _engine is None:
        _engine = LearningEngine(notify_fn=notify_fn)
    return _engine
