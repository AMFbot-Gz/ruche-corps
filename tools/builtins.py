"""
tools/builtins.py — Arsenal complet de La Ruche (28 outils)
Tout ce dont un humain spécialiste a besoin pour travailler sur un Mac.
"""
import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

import httpx

from config import CFG
from tools.registry import tool

# ═══════════════════════════════════════════════════════════════
# SYSTÈME
# ═══════════════════════════════════════════════════════════════

@tool("Exécuter une commande shell macOS de façon sécurisée", "system")
async def shell(command: str, cwd: str = "", timeout: int = 60) -> str:
    """
    command: commande shell à exécuter
    cwd: répertoire de travail (défaut: ruche-corps)
    timeout: timeout en secondes (max 60)
    """
    from computer.sandbox import run
    r = await run(command, cwd=cwd or None, timeout=min(timeout, 60))
    if r["blocked"]:
        return f"BLOQUÉ: {r['stderr']}"
    out = r["stdout"] or r["stderr"] or "(aucune sortie)"
    return out[:4000]


@tool("Exécuter du code Python et retourner le résultat", "system")
async def run_python(code: str) -> str:
    """code: code Python à exécuter"""
    from computer.sandbox import run
    safe_code = code.replace("'", "'\"'\"'")
    r = await run(f"python3 -c '{safe_code}'", timeout=30)
    return (r["stdout"] or r["stderr"] or "(aucune sortie)")[:3000]


@tool("Infos système : CPU, RAM, disque, processus actifs", "system")
async def system_info() -> str:
    """Retourne un snapshot complet de l'état du système."""
    import psutil, platform
    cpu   = psutil.cpu_percent(interval=0.5)
    mem   = psutil.virtual_memory()
    disk  = psutil.disk_usage("/")
    procs = sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda p: p.info["cpu_percent"] or 0, reverse=True
    )[:8]
    lines = [
        f"Système: {platform.node()} — macOS {platform.mac_ver()[0]}",
        f"CPU: {cpu}% | RAM: {mem.percent}% ({mem.used//1e9:.1f}/{mem.total//1e9:.1f} GB)",
        f"Disque: {disk.used//1e9:.1f}/{disk.total//1e9:.1f} GB ({disk.percent}%)",
        "Top processus:",
    ] + [
        f"  PID {p.info['pid']} {p.info['name']} — CPU {p.info['cpu_percent']:.1f}%"
        for p in procs
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# FICHIERS
# ═══════════════════════════════════════════════════════════════

@tool("Lire le contenu d'un fichier", "files")
async def read_file(path: str, lines: int = 200) -> str:
    """
    path: chemin du fichier
    lines: nombre de lignes max (défaut 200)
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"Fichier introuvable: {path}"
    try:
        content    = p.read_text(errors="replace")
        all_lines  = content.splitlines()
        if len(all_lines) > lines:
            return "\n".join(all_lines[:lines]) + f"\n\n[...{len(all_lines)-lines} lignes de plus]"
        return content
    except Exception as e:
        return f"Erreur lecture: {e}"


@tool("Écrire ou remplacer un fichier complet", "files")
async def write_file(path: str, content: str) -> str:
    """
    path: chemin du fichier
    content: contenu complet à écrire
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"✅ Écrit: {path} ({len(content)} chars)"


@tool("Modifier une section d'un fichier (chercher et remplacer)", "files")
async def edit_file(path: str, old_text: str, new_text: str) -> str:
    """
    path: chemin du fichier
    old_text: texte exact à remplacer
    new_text: nouveau texte de remplacement
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"Fichier introuvable: {path}"
    content = p.read_text(errors="replace")
    if old_text not in content:
        return f"Texte non trouvé dans {path}"
    p.write_text(content.replace(old_text, new_text, 1))
    return f"✅ Modifié: {path}"


@tool("Lister le contenu d'un répertoire", "files")
async def list_dir(path: str = ".", depth: int = 2) -> str:
    """
    path: répertoire à lister
    depth: profondeur max (défaut 2)
    """
    from computer.sandbox import run
    r = await run(
        f"find {shlex.quote(path)} -maxdepth {depth} "
        "-not -path '*/node_modules/*' -not -path '*/__pycache__/*' "
        "-not -path '*/.git/*' | sort | head -80"
    )
    return r["stdout"] or r["stderr"] or f"Répertoire vide: {path}"


@tool("Chercher des fichiers par pattern glob", "files")
async def find_files(pattern: str, root: str = ".", max_results: int = 30) -> str:
    """
    pattern: pattern glob (ex: *.py, **/*.ts)
    root: répertoire de base
    max_results: nombre max de résultats
    """
    import glob
    matches = glob.glob(os.path.join(root, "**", pattern), recursive=True)
    matches = [m for m in matches if "node_modules" not in m and "__pycache__" not in m][:max_results]
    return "\n".join(matches) if matches else f"Aucun fichier: {pattern}"


@tool("Charger plusieurs fichiers dans le contexte 1M tokens de Nemotron pour analyse", "files")
async def load_context(paths: str, query: str = "") -> str:
    """
    paths: chemins séparés par virgule OU répertoire projet
    query: requête pour auto-sélection intelligente des fichiers
    """
    from context.builder import ContextBuilder
    cb         = ContextBuilder()
    path_list  = [p.strip() for p in paths.split(",")]
    p0         = Path(path_list[0]).expanduser()
    if len(path_list) == 1 and p0.is_dir():
        if query:
            auto = cb.auto_files_for_query(query, str(p0))
            return cb.build(query=query, files=auto)
        return cb.build(query=query, projects=[str(p0)])
    return cb.build(query=query, files=[str(Path(p).expanduser()) for p in path_list])


# ═══════════════════════════════════════════════════════════════
# WEB
# ═══════════════════════════════════════════════════════════════

@tool("Rechercher sur le web (DuckDuckGo)", "web")
async def web_search(query: str, max_results: int = 5) -> str:
    """
    query: requête de recherche
    max_results: nombre de résultats souhaités
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = r.json()
        results = []
        if data.get("AbstractText"):
            results.append(f"📋 {data['AbstractText'][:500]}")
        for item in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(item, dict) and "Text" in item:
                results.append(f"• {item['Text'][:200]}")
        return "\n".join(results) if results else f"Pas de résultats pour: {query}"
    except Exception as e:
        return f"Erreur recherche: {e}"


@tool("Récupérer le contenu textuel d'une URL web", "web")
async def web_fetch(url: str, extract_text: bool = True) -> str:
    """
    url: URL à récupérer
    extract_text: extraire uniquement le texte HTML (défaut True)
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if not extract_text:
            return r.text[:5000]
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", "", r.text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>",   "", text,   flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:5000]
    except Exception as e:
        return f"Erreur fetch {url}: {e}"


# ═══════════════════════════════════════════════════════════════
# COMPUTER USE
# ═══════════════════════════════════════════════════════════════

@tool("Voir et analyser l'écran avec vision IA (Nemotron + llava)", "computer")
async def see_screen(question: str = "Décris l'écran en détail.") -> str:
    """question: question spécifique à poser sur l'écran"""
    from computer.screen import see
    result = await see(question)
    if result.get("error"):
        return f"Erreur vision: {result['error']}"
    changed = " 📍 Écran modifié." if result["changed"] else ""
    return f"{result['description']}{changed}"


@tool("Cliquer à des coordonnées précises sur l'écran", "computer")
async def click(x: int, y: int, button: str = "left") -> str:
    """
    x: coordonnée X pixels | y: coordonnée Y pixels
    button: left / right / middle
    """
    from computer.input import click as _click
    r = await _click(x, y, button=button)
    return f"✅ Clic ({x},{y})" if r["ok"] else f"❌ {r['error']}"


@tool("Double-cliquer sur l'écran", "computer")
async def double_click(x: int, y: int) -> str:
    """x: coordonnée X | y: coordonnée Y"""
    from computer.input import double_click as _dc
    r = await _dc(x, y)
    return f"✅ Double-clic ({x},{y})" if r["ok"] else f"❌ {r['error']}"


@tool("Taper du texte au clavier — supporte accents et Unicode", "computer")
async def type_text(text: str) -> str:
    """text: texte à taper (accents, emojis supportés via clipboard)"""
    from computer.input import type_text as _type
    r = await _type(text)
    return f"✅ Tapé {r.get('chars', len(text))} chars" if r["ok"] else f"❌ {r['error']}"


@tool("Appuyer sur un raccourci clavier", "computer")
async def hotkey(keys: str) -> str:
    """keys: touches séparées par + (ex: command+c, ctrl+shift+esc, command+space)"""
    from computer.input import hotkey as _hotkey
    key_list = [k.strip() for k in keys.replace(" ", "").split("+")]
    r = await _hotkey(*key_list)
    return f"✅ Raccourci {keys}" if r["ok"] else f"❌ {r['error']}"


@tool("Déplacer la souris vers des coordonnées", "computer")
async def move_mouse(x: int, y: int) -> str:
    """x: coordonnée X | y: coordonnée Y"""
    from computer.input import move
    r = await move(x, y)
    return f"✅ Souris → ({x},{y})" if r["ok"] else f"❌ {r['error']}"


@tool("Faire défiler la page (scroll)", "computer")
async def scroll(x: int, y: int, clicks: int = 3) -> str:
    """
    x: position X | y: position Y
    clicks: crans (positif=haut, négatif=bas)
    """
    from computer.input import scroll as _scroll
    r = await _scroll(x, y, clicks)
    return f"✅ Scroll {clicks:+d} crans" if r["ok"] else f"❌ {r['error']}"


@tool("Ouvrir ou mettre au premier plan une application macOS", "computer")
async def open_app(app_name: str, focus_only: bool = False) -> str:
    """
    app_name: nom app (Safari, Terminal, Finder, VS Code...)
    focus_only: juste focus sans ouvrir si False
    """
    from computer.input import open_app as _open, focus_app
    r = await (focus_app if focus_only else _open)(app_name)
    return f"✅ {app_name}" if r["ok"] else f"❌ {r['error']}"


@tool("Exécuter un script AppleScript macOS", "computer")
async def applescript(script: str) -> str:
    """script: code AppleScript (ex: tell app 'Finder' to open home)"""
    from computer.input import run_applescript
    r = await run_applescript(script)
    if r["ok"]:
        return r["stdout"] or "✅ OK"
    return f"❌ {r['stderr']}"


# ═══════════════════════════════════════════════════════════════
# CODE
# ═══════════════════════════════════════════════════════════════

@tool("Éditer du code avec aider+qwen3-coder — Claude Code local open source", "code")
async def code_edit(repo_path: str, instruction: str) -> str:
    """
    repo_path: chemin du dépôt à modifier
    instruction: description précise de la modification
    """
    p = Path(repo_path).expanduser()
    if not p.exists():
        return f"Répertoire introuvable: {repo_path}"
    cmd = (
        f"cd {shlex.quote(str(p))} && "
        f"OLLAMA_API_BASE={CFG.OLLAMA}/v1 "
        f"aider --model ollama/{CFG.M_CODE} "
        f"--no-git --yes --no-stream --no-check-update "
        f"--message {shlex.quote(instruction)} 2>&1 | tail -30"
    )
    from computer.sandbox import run
    r = await run(cmd, timeout=120)
    return r["stdout"] or r["stderr"] or "Aider: aucune sortie"


@tool("Analyser un fichier ou projet de code avec contexte complet", "code")
async def analyze_code(path: str, question: str = "Identifie les problèmes et donne des améliorations") -> str:
    """
    path: fichier ou répertoire à analyser
    question: question spécifique sur le code
    """
    from context.builder import ContextBuilder
    cb  = ContextBuilder()
    p   = Path(path).expanduser()
    ctx = cb.load_project(p) if p.is_dir() else cb.load_file(p)
    if not ctx:
        return f"Impossible de charger: {path}"
    return f"Contexte chargé ({len(ctx)} chars).\nQuestion: {question}\n\n{ctx[:3000]}"


# ═══════════════════════════════════════════════════════════════
# GITHUB
# ═══════════════════════════════════════════════════════════════

@tool("GitHub : repos, issues, PRs, recherche de code", "github")
async def github(action: str, params: str = "") -> str:
    """
    action: list_repos | list_issues | create_issue | create_pr | search_code
    params: JSON (ex: {"repo":"owner/name","title":"bug","body":"description"})
    """
    try:
        p = json.loads(params) if params else {}
    except Exception:
        p = {"query": params}

    hdrs = {"Accept": "application/vnd.github.v3+json"}
    if CFG.GITHUB_TK:
        hdrs["Authorization"] = f"token {CFG.GITHUB_TK}"

    async with httpx.AsyncClient(timeout=20.0) as c:
        if action == "list_repos":
            r     = await c.get("https://api.github.com/user/repos?sort=updated&per_page=20", headers=hdrs)
            repos = r.json()
            return "\n".join(f"• {repo['full_name']}" for repo in repos[:15])

        if action == "list_issues":
            r      = await c.get(f"https://api.github.com/repos/{p.get('repo','')}/issues?state=open&per_page=10", headers=hdrs)
            issues = r.json()
            return "\n".join(f"#{i['number']} {i['title']}" for i in issues[:10])

        if action == "create_issue":
            r    = await c.post(f"https://api.github.com/repos/{p.get('repo','')}/issues",
                                headers=hdrs, json={"title": p.get("title",""), "body": p.get("body","")})
            data = r.json()
            return f"Issue #{data.get('number')} créée: {data.get('html_url','')}"

        if action == "search_code":
            r     = await c.get("https://api.github.com/search/code",
                                headers=hdrs, params={"q": p.get("query",""), "per_page": 5})
            items = r.json().get("items", [])
            return "\n".join(f"• {i['repository']['full_name']}/{i['path']}" for i in items)

        return f"Action inconnue: {action}. Disponibles: list_repos, list_issues, create_issue, create_pr, search_code"


# ═══════════════════════════════════════════════════════════════
# GHOST OS
# ═══════════════════════════════════════════════════════════════

@tool("Lancer une mission Ghost OS Ultimate", "ghost")
async def ghost_mission(mission: str, priority: int = 3) -> str:
    """
    mission: description de la mission
    priority: priorité 1-5 (3 = normal)
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(f"{CFG.GHOST_URL}/api/mission",
                             headers={"X-Ghost-Secret": CFG.GHOST_SEC or ""},
                             json={"mission": mission, "priority": priority})
            return json.dumps(r.json(), ensure_ascii=False, indent=2)[:1000]
    except Exception as e:
        return f"Ghost OS indisponible: {e}"


@tool("Statut complet de Ghost OS Ultimate", "ghost")
async def ghost_status() -> str:
    """Retourne uptime, missions, statut Ghost OS."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{CFG.GHOST_URL}/api/status",
                            headers={"X-Ghost-Secret": CFG.GHOST_SEC or ""})
            d = r.json()
        return (f"Ghost OS — Uptime: {d.get('uptime',0)//3600}h | "
                f"Missions: {d.get('missions',{}).get('total',0)} | "
                f"Statut: {d.get('status','?')}")
    except Exception as e:
        return f"Ghost OS indisponible: {e}"


# ═══════════════════════════════════════════════════════════════
# IA
# ═══════════════════════════════════════════════════════════════

@tool("Lister tous les modèles Ollama disponibles", "ai")
async def list_models() -> str:
    """Retourne les modèles Ollama locaux et cloud avec leur taille."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r      = await c.get(f"{CFG.OLLAMA}/api/tags")
            models = r.json().get("models", [])
        lines = []
        for m in sorted(models, key=lambda x: x.get("size", 0), reverse=True):
            gb   = m.get("size", 0) / 1e9
            tag  = "☁️ cloud" if gb < 0.1 else f"{gb:.1f} GB"
            lines.append(f"  {tag:>10}  {m['name']}")
        return f"{len(models)} modèles disponibles:\n" + "\n".join(lines)
    except Exception as e:
        return f"Erreur Ollama: {e}"


@tool("Réponse enrichie : 3 modèles en parallèle + synthèse Nemotron", "ai")
async def mixture_answer(question: str) -> str:
    """question: question complexe à analyser sous plusieurs angles simultanément"""
    async def ask(model: str, q: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(f"{CFG.OLLAMA}/api/chat", json={
                    "model": model,
                    "messages": [{"role": "user", "content": q}],
                    "stream": False,
                    "options": {"temperature": 0.7, "num_predict": 500},
                })
            return r.json().get("message", {}).get("content", "")
        except Exception as e:
            return f"[{model} indisponible: {e}]"

    models  = [CFG.M_GENERAL, CFG.M_CODE, CFG.M_FAST]
    answers = await asyncio.gather(*[ask(m, question) for m in models])
    labeled = "\n\n".join(f"**{m}:**\n{a}" for m, a in zip(models, answers) if a)

    # Synthèse Nemotron avec grand contexte
    synth = (
        f"Voici 3 perspectives sur: '{question}'\n\n{labeled}\n\n"
        "Synthétise la meilleure réponse en combinant les insights clés de chaque modèle."
    )
    try:
        async with httpx.AsyncClient(timeout=90.0) as c:
            r = await c.post(f"{CFG.OLLAMA}/api/chat", json={
                "model": CFG.M_GENERAL,
                "messages": [{"role": "user", "content": synth}],
                "stream": False,
                "options": {"temperature": 0.5, "num_predict": 800,
                            "num_ctx": CFG.NEMOTRON_CTX},
            })
        return r.json().get("message", {}).get("content", labeled)
    except Exception:
        return labeled


# ═══════════════════════════════════════════════════════════════
# MÉMOIRE
# ═══════════════════════════════════════════════════════════════

@tool("Mémoriser un fait important de façon permanente", "memory")
async def remember(fact: str, category: str = "general") -> str:
    """
    fact: information à mémoriser
    category: catégorie (general, user, project, tech)
    """
    try:
        import redis.asyncio as aioredis
        r   = await aioredis.from_url(CFG.REDIS)
        key = f"ruche:fact:{abs(hash(fact)) % 1_000_000}"
        await r.setex(key, 86400 * 30, json.dumps({"fact": fact, "category": category}))
        await r.aclose()
    except Exception:
        pass
    return f"✅ Mémorisé [{category}]: {fact[:100]}"


@tool("Rappeler des souvenirs ou faits mémorisés", "memory")
async def recall(query: str) -> str:
    """query: mot-clé ou sujet à rechercher dans la mémoire"""
    try:
        import redis.asyncio as aioredis
        r     = await aioredis.from_url(CFG.REDIS)
        keys  = await r.keys("ruche:fact:*")
        facts = []
        for k in keys[:100]:
            d = await r.get(k)
            if d:
                obj = json.loads(d)
                if query.lower() in obj.get("fact", "").lower():
                    facts.append(f"[{obj.get('category','?')}] {obj['fact']}")
        await r.aclose()
        return "\n".join(facts) if facts else f"Aucun souvenir pour: {query}"
    except Exception as e:
        return f"Mémoire indisponible: {e}"
