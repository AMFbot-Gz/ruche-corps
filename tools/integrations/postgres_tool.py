"""
Intégration PostgreSQL — requêtes SQL directes sur revenue-os-postgres
Credentials: postgresql://revenue_os:changeme123@localhost:5432/revenue_os
Utilise asyncpg (async natif). Fallback docker exec si connexion impossible.
"""
import asyncio
import json
import subprocess
from typing import Optional

from config import CFG


async def _get_conn():
    """Crée une connexion asyncpg vers Postgres."""
    import asyncpg
    return await asyncpg.connect(CFG.POSTGRES_URL)


async def query(sql: str, params: list = None) -> list[dict]:
    """
    Exécute une requête SQL SELECT et retourne les résultats comme liste de dicts.
    Fallback sur docker exec si asyncpg échoue.
    """
    try:
        import asyncpg
        conn = await _get_conn()
        try:
            if params:
                rows = await conn.fetch(sql, *params)
            else:
                rows = await conn.fetch(sql)
            return [dict(row) for row in rows]
        finally:
            await conn.close()
    except Exception as e:
        # Fallback docker exec
        return _docker_query(sql)


async def execute(sql: str, params: list = None) -> int:
    """
    Exécute un INSERT/UPDATE/DELETE et retourne le nombre de lignes affectées.
    """
    try:
        import asyncpg
        conn = await _get_conn()
        try:
            if params:
                result = await conn.execute(sql, *params)
            else:
                result = await conn.execute(sql)
            # asyncpg retourne "INSERT 0 N" ou "UPDATE N" etc.
            parts = result.split()
            return int(parts[-1]) if parts and parts[-1].isdigit() else 0
        finally:
            await conn.close()
    except Exception as e:
        return _docker_execute(sql)


async def list_tables(database: str = "revenue_os") -> list[str]:
    """Liste toutes les tables d'une base de données (schéma public)."""
    rows = await query(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    return [r["tablename"] for r in rows]


async def describe_table(table: str) -> list[dict]:
    """Retourne la structure d'une table (colonnes, types, nullable)."""
    rows = await query(
        """
        SELECT
            column_name,
            data_type,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = $1
        ORDER BY ordinal_position
        """,
        [table],
    )
    return rows


# ─── Fallback docker exec ─────────────────────────────────────────────────────

def _docker_psql(sql: str) -> str:
    """Exécute SQL via docker exec en fallback."""
    result = subprocess.run(
        [
            "docker", "exec", "revenue-os-postgres",
            "psql", "-U", "revenue_os", "-d", "revenue_os",
            "-t", "-A", "-F", "\t",
            "-c", sql,
        ],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def _docker_query(sql: str) -> list[dict]:
    """Requête SELECT via docker exec, retourne list[dict] (colonnes auto-détectées)."""
    output = _docker_psql(sql)
    if not output:
        return []
    lines = output.splitlines()
    rows = []
    for line in lines:
        cells = line.split("\t")
        rows.append({"col_" + str(i): v for i, v in enumerate(cells)})
    return rows


def _docker_execute(sql: str) -> int:
    """INSERT/UPDATE/DELETE via docker exec, retourne nb lignes affectées."""
    output = _docker_psql(sql)
    parts = output.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0
