"""
computer/sandbox.py — Shell sécurisé de La Ruche
Double-couche : regex + shlex tokenisation
Patterns bloqués + timeout + troncature sortie
"""
import asyncio
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

# ─── Patterns BLOQUÉS (jamais exécutés) ─────────────────────
_BLOCKED = [
    re.compile(r'rm\s+.*-.*r.*\s+/', re.IGNORECASE),
    re.compile(r'rm\s+--recursive', re.IGNORECASE),
    re.compile(r':\s*\(\s*\)\s*\{.*\|.*\}', re.DOTALL),       # fork bomb
    re.compile(r'dd\s+if=/dev/zero', re.IGNORECASE),
    re.compile(r'\bmkfs\b', re.IGNORECASE),
    re.compile(r'\b(shutdown|reboot|poweroff|halt)\b', re.IGNORECASE),
    re.compile(r'>\s*/etc/(passwd|shadow)', re.IGNORECASE),
    re.compile(r'curl.*\|\s*(bash|sh|zsh)', re.IGNORECASE),
    re.compile(r'wget.*-O.*\|\s*(bash|sh|zsh)', re.IGNORECASE),
]

# ─── Commandes sûres (pas de confirmation requise) ───────────
_SAFE_CMDS = frozenset([
    'ls', 'cat', 'grep', 'find', 'head', 'tail', 'wc', 'echo', 'pwd',
    'which', 'ps', 'df', 'du', 'curl', 'wget', 'git', 'node', 'python3',
    'npm', 'pip3', 'pip', 'make', 'lsof', 'date', 'uname', 'env', 'id',
    'whoami', 'hostname', 'uptime', 'top', 'htop', 'netstat', 'ping',
    'open', 'osascript', 'screencapture', 'aider', 'gh', 'brew',
])

CMD_MAX   = 3000
OUT_MAX   = 15_000
TIMEOUT   = 60


def is_blocked(cmd: str) -> bool:
    if len(cmd) > CMD_MAX:
        return True
    normalized = " ".join(cmd.split())
    if any(p.search(normalized) for p in _BLOCKED):
        return True
    try:
        tokens = shlex.split(normalized, posix=True)
        joined = " ".join(tokens)
        if any(p.search(joined) for p in _BLOCKED):
            return True
        if tokens:
            base = tokens[0].split("/")[-1]
            if base in {"mkfs", "dd", "shutdown", "reboot", "poweroff", "halt"}:
                return True
            if base == "rm":
                flags = [t for t in tokens[1:] if t.startswith("-")]
                paths = [t for t in tokens[1:] if not t.startswith("-")]
                has_r = any("r" in f.lstrip("-").lower() for f in flags)
                bad_path = any(p.startswith("/") and any(
                    p.startswith(s) for s in
                    ["/etc", "/usr", "/bin", "/sbin", "/lib", "/var", "/System", "/Library"]
                ) for p in paths)
                if has_r and bad_path:
                    return True
    except ValueError:
        return True  # shlex fail = blocage par précaution
    return False


async def run(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = TIMEOUT,
    env_extra: Optional[dict] = None,
) -> dict:
    """
    Exécute une commande shell de façon sécurisée.
    Retourne { stdout, stderr, returncode, blocked, truncated }
    """
    if is_blocked(command):
        return {
            "stdout": "",
            "stderr": f"Commande bloquée par sandbox: {command[:200]}",
            "returncode": -1,
            "blocked": True,
        }

    work_dir = cwd or str(Path.home() / "Projects" / "ruche-corps")
    import os
    run_env = os.environ.copy()
    if env_extra:
        run_env.update(env_extra)

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=min(timeout, TIMEOUT), cwd=work_dir, env=run_env,
        )
        stdout   = result.stdout[:OUT_MAX]
        stderr   = result.stderr[:OUT_MAX]
        trunc    = (len(result.stdout) > OUT_MAX or len(result.stderr) > OUT_MAX)
        return {
            "stdout":      stdout,
            "stderr":      stderr,
            "returncode":  result.returncode,
            "blocked":     False,
            "truncated":   trunc,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "", "stderr": f"Timeout ({timeout}s)",
            "returncode": -1, "blocked": False,
        }
    except Exception as e:
        return {
            "stdout": "", "stderr": str(e)[:500],
            "returncode": -1, "blocked": False,
        }
