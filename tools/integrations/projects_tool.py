"""
tools/integrations/projects_tool.py — Gestion des projets locaux

Vision d'ensemble de ~/Projects/ :
  - Détection automatique du type (python/node/mixed/unknown)
  - Statut git (branche courante, derniers commits)
  - Détection des services actifs (via .env ou ports)
  - Recherche de code cross-projets via ripgrep/grep
  - Ouverture dans Cursor / VSCode / Finder
"""
import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Constantes ──────────────────────────────────────────────────────────────

PROJECTS_DIR = Path.home() / "Projects"

# Dossiers à ignorer lors du listing
IGNORE_DIRS = {"Archive", "node_modules", "__pycache__", ".git", ".venv",
               "venv", "dist", "build", ".next", ".turbo", "target"}

# Fichiers signatures pour détecter le type de projet
_PY_SIGNATURES    = {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile"}
_NODE_SIGNATURES  = {"package.json"}
_RUST_SIGNATURES  = {"Cargo.toml"}
_GO_SIGNATURES    = {"go.mod"}


# ─── Helpers internes ────────────────────────────────────────────────────────

async def _run(cmd: str, cwd: str = None, timeout: int = 10) -> dict:
    """Exécute une commande shell de façon asynchrone."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode(errors="replace").strip(),
            "stderr": stderr.decode(errors="replace").strip(),
            "ok": proc.returncode == 0,
        }
    except asyncio.TimeoutError:
        return {"stdout": "", "stderr": f"Timeout ({timeout}s)", "ok": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "ok": False}


def _detect_type(root: Path) -> str:
    """Détecte le type de projet selon les fichiers signatures présents."""
    files = {f.name for f in root.iterdir() if f.is_file()} if root.exists() else set()
    has_py   = bool(files & _PY_SIGNATURES)
    has_node = bool(files & _NODE_SIGNATURES)
    has_rust = bool(files & _RUST_SIGNATURES)
    has_go   = bool(files & _GO_SIGNATURES)

    types = []
    if has_py:   types.append("python")
    if has_node: types.append("node")
    if has_rust: types.append("rust")
    if has_go:   types.append("go")

    if len(types) == 0:   return "unknown"
    if len(types) == 1:   return types[0]
    return "mixed"


async def _git_branch(path: Path) -> str:
    """Retourne la branche git courante, ou '' si pas un repo."""
    r = await _run("git rev-parse --abbrev-ref HEAD", cwd=str(path))
    return r["stdout"] if r["ok"] else ""


async def _git_log(path: Path, n: int = 5) -> str:
    """Retourne les n derniers commits (oneline)."""
    r = await _run(f"git log --oneline -{n}", cwd=str(path))
    return r["stdout"] if r["ok"] else ""


async def _detect_running(path: Path) -> bool:
    """
    Tente de détecter si un service du projet tourne.
    Lit le .env pour trouver un PORT, puis vérifie avec lsof.
    """
    env_file = path / ".env"
    port = None

    if env_file.exists():
        try:
            for line in env_file.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("PORT=") or line.startswith("APP_PORT="):
                    port = line.split("=", 1)[1].strip().split("#")[0].strip()
                    break
        except Exception:
            pass

    if port and port.isdigit():
        r = await _run(f"lsof -ti tcp:{port}", timeout=5)
        return bool(r["stdout"])

    return False


# ─── API publique ────────────────────────────────────────────────────────────

async def list_projects() -> list[dict]:
    """
    Liste tous les projets dans ~/Projects/ avec :
    - name          : nom du répertoire
    - path          : chemin absolu
    - type          : python | node | mixed | rust | go | unknown
    - git_branch    : branche courante ('' si non-git)
    - last_modified : date de dernière modification (ISO)
    - running       : bool — service actif sur un port
    """
    if not PROJECTS_DIR.exists():
        return []

    projects = []

    # Filtrer les sous-dossiers directs (pas récursif)
    dirs = [
        d for d in PROJECTS_DIR.iterdir()
        if d.is_dir() and d.name not in IGNORE_DIRS and not d.name.startswith(".")
    ]

    # Traitement parallèle pour la performance
    async def _process(d: Path) -> dict:
        proj_type = _detect_type(d)
        branch    = await _git_branch(d)
        running   = await _detect_running(d)
        mtime     = datetime.fromtimestamp(d.stat().st_mtime).isoformat()

        return {
            "name":          d.name,
            "path":          str(d),
            "type":          proj_type,
            "git_branch":    branch,
            "last_modified": mtime,
            "running":       running,
        }

    results = await asyncio.gather(*[_process(d) for d in dirs])
    # Trier par date de modification (le plus récent en premier)
    projects = sorted(results, key=lambda p: p["last_modified"], reverse=True)
    return projects


async def project_status(name: str) -> dict:
    """
    Statut détaillé d'un projet :
    - path         : chemin absolu
    - type         : type de projet
    - main_files   : fichiers principaux détectés
    - dependencies : résumé des dépendances (package.json ou requirements.txt)
    - git_log      : 5 derniers commits
    - git_status   : git status court
    - running      : service actif
    - scripts      : scripts disponibles (si package.json)
    """
    # Chercher le projet
    path = PROJECTS_DIR / name
    if not path.exists():
        # Chercher insensible à la casse
        matches = [d for d in PROJECTS_DIR.iterdir()
                   if d.is_dir() and d.name.lower() == name.lower()]
        if matches:
            path = matches[0]
        else:
            return {"error": f"Projet '{name}' introuvable dans {PROJECTS_DIR}"}

    proj_type = _detect_type(path)

    # Fichiers principaux
    main_patterns = ["main.py", "app.py", "index.ts", "index.js", "src/index.ts",
                     "src/index.js", "main.ts", "server.py", "server.ts",
                     "README.md", "docker-compose.yml"]
    main_files = [str(path / f) for f in main_patterns if (path / f).exists()]

    # Dépendances
    deps_summary = ""
    pkg_json = path / "package.json"
    req_txt  = path / "requirements.txt"
    pyproject = path / "pyproject.toml"

    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text())
            dep_count  = len(data.get("dependencies", {}))
            dev_count  = len(data.get("devDependencies", {}))
            deps_summary = f"npm: {dep_count} deps + {dev_count} devDeps"
            # Ajouter les scripts
            scripts = list(data.get("scripts", {}).keys())
        except Exception:
            scripts = []
    else:
        scripts = []

    if req_txt.exists():
        try:
            lines = [l.strip() for l in req_txt.read_text().splitlines()
                     if l.strip() and not l.startswith("#")]
            deps_summary = f"pip: {len(lines)} paquets"
        except Exception:
            pass

    if pyproject.exists() and not deps_summary:
        deps_summary = "pyproject.toml présent"

    # Git
    git_log, git_status = await asyncio.gather(
        _git_log(path),
        _run("git status --short", cwd=str(path)),
    )
    running = await _detect_running(path)

    return {
        "name":         name,
        "path":         str(path),
        "type":         proj_type,
        "main_files":   main_files,
        "dependencies": deps_summary or "(aucun fichier de dépendances détecté)",
        "scripts":      scripts[:10],
        "git_log":      git_log,
        "git_status":   git_status["stdout"] if git_status["ok"] else "(non-git)",
        "running":      running,
    }


async def open_project(name: str, editor: str = "cursor") -> str:
    """
    Ouvre un projet dans l'éditeur spécifié.
    editor: 'cursor' | 'code' | 'finder'
    """
    path = PROJECTS_DIR / name
    if not path.exists():
        return f"Projet '{name}' introuvable dans {PROJECTS_DIR}"

    editor_map = {
        "cursor": ["open", "-a", "Cursor", str(path)],
        "code":   ["open", "-a", "Visual Studio Code", str(path)],
        "finder": ["open", str(path)],
    }

    cmd = editor_map.get(editor.lower())
    if not cmd:
        return f"Éditeur inconnu: {editor}. Disponibles: cursor, code, finder"

    r = await _run(" ".join(f'"{c}"' if " " in c else c for c in cmd), timeout=10)
    if r["ok"] or not r["stderr"]:
        return f"Projet '{name}' ouvert dans {editor}"
    return f"Erreur ouverture: {r['stderr']}"


async def project_git_status(name: str) -> str:
    """Git status + log des 5 derniers commits d'un projet."""
    path = PROJECTS_DIR / name
    if not path.exists():
        return f"Projet '{name}' introuvable"

    status, log = await asyncio.gather(
        _run("git status", cwd=str(path)),
        _git_log(path),
    )

    parts = []
    if status["ok"]:
        parts.append(f"=== git status ===\n{status['stdout']}")
    else:
        parts.append("(pas un dépôt git ou git indisponible)")

    if log:
        parts.append(f"\n=== 5 derniers commits ===\n{log}")

    return "\n".join(parts) if parts else "Aucune info git disponible"


async def search_in_projects(query: str, file_type: str = "") -> list[dict]:
    """
    Cherche du texte dans tous les projets locaux.
    query     : texte à chercher (regex supporté)
    file_type : extension sans point ('py', 'ts', 'js', '') — vide = tous
    Retourne  : [{project, file, line, content}]
    Utilise ripgrep si disponible, sinon grep.
    """
    if not PROJECTS_DIR.exists():
        return []

    # Choisir le moteur de recherche
    rg_available = subprocess.run(
        ["which", "rg"], capture_output=True
    ).returncode == 0

    results = []

    if rg_available:
        # ripgrep : rapide et coloré
        type_flag = f"--type {file_type}" if file_type else ""
        cmd = (
            f"rg --json -l {type_flag} "
            f"--glob '!node_modules' --glob '!.git' --glob '!__pycache__' "
            f"--glob '!dist' --glob '!build' --glob '!.venv' "
            f"{_shell_quote(query)} {str(PROJECTS_DIR)}"
        )
        r = await _run(cmd, timeout=20)
        if r["ok"]:
            for line in r["stdout"].splitlines():
                try:
                    data = json.loads(line)
                    if data.get("type") == "match":
                        m = data["data"]
                        file_path = m["path"]["text"]
                        project   = _project_name_from_path(file_path)
                        for sub in m.get("submatches", []):
                            results.append({
                                "project": project,
                                "file":    file_path,
                                "line":    m.get("line_number", 0),
                                "content": m.get("lines", {}).get("text", "").strip(),
                            })
                except (json.JSONDecodeError, KeyError):
                    pass
    else:
        # grep fallback
        include_flag = f"--include='*.{file_type}'" if file_type else ""
        cmd = (
            f"grep -rn {include_flag} "
            f"--exclude-dir=node_modules --exclude-dir=.git "
            f"--exclude-dir=__pycache__ --exclude-dir=dist "
            f"{_shell_quote(query)} {str(PROJECTS_DIR)}"
        )
        r = await _run(cmd, timeout=20)
        if r["stdout"]:
            for line in r["stdout"].splitlines()[:50]:
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    file_path = parts[0]
                    project   = _project_name_from_path(file_path)
                    results.append({
                        "project": project,
                        "file":    file_path,
                        "line":    int(parts[1]) if parts[1].isdigit() else 0,
                        "content": parts[2].strip(),
                    })

    return results[:100]  # Limiter à 100 résultats


def _shell_quote(s: str) -> str:
    """Échappe une chaîne pour l'utiliser dans un shell."""
    import shlex
    return shlex.quote(s)


def _project_name_from_path(file_path: str) -> str:
    """Extrait le nom du projet depuis un chemin absolu."""
    try:
        rel = Path(file_path).relative_to(PROJECTS_DIR)
        return rel.parts[0] if rel.parts else "?"
    except ValueError:
        return "?"
