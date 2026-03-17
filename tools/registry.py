"""
tools/registry.py — Registre d'outils dynamique de La Ruche

Amélioration vs PicoClaw :
  PicoClaw = outils compilés dans le binaire Go (impossible d'ajouter sans rebuild)
  Ruche    = @tool decorator → registration à chaud, n'importe où dans le code

Usage :
    from tools.registry import tool, registry

    @tool(description="Exécuter une commande shell")
    async def shell_exec(command: str, timeout: int = 30) -> str:
        ...
"""
import asyncio
import inspect
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ─── Décorateur @tool ─────────────────────────────────────────────────────────
def tool(description: str, category: str = "general", name: str = None):
    """Décorateur pour enregistrer une fonction comme outil LLM."""
    def decorator(fn: Callable):
        fn._tool_meta = ToolMeta(
            name=name or fn.__name__,
            description=description,
            category=category,
            fn=fn,
            schema=_build_schema(fn, description),
        )
        registry.register(fn._tool_meta)
        return fn
    return decorator


# ─── Métadonnées d'outil ──────────────────────────────────────────────────────
@dataclass
class ToolMeta:
    name:        str
    description: str
    category:    str
    fn:          Callable
    schema:      dict


# ─── Schéma JSON depuis annotations Python ────────────────────────────────────
_PY_TO_JSON = {
    "str":   "string",
    "int":   "integer",
    "float": "number",
    "bool":  "boolean",
    "list":  "array",
    "dict":  "object",
}

def _build_schema(fn: Callable, description: str) -> dict:
    sig    = inspect.signature(fn)
    hints  = {}
    try:
        hints = fn.__annotations__
    except Exception:
        pass
    props    = {}
    required = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "ctx"):
            continue
        type_hint  = hints.get(pname, str)
        type_name  = getattr(type_hint, "__name__", str(type_hint))
        json_type  = _PY_TO_JSON.get(type_name, "string")
        doc_lines  = (fn.__doc__ or "").strip().splitlines()
        param_desc = next(
            (l.strip().lstrip(f"{pname}:").strip()
             for l in doc_lines if l.strip().startswith(f"{pname}:")),
            pname,
        )
        props[pname] = {"type": json_type, "description": param_desc}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "type": "function",
        "function": {
            "name":        fn.__name__ if not hasattr(fn, "_tool_meta") else fn._tool_meta.name if hasattr(fn, "_tool_meta") else fn.__name__,
            "description": description,
            "parameters": {
                "type":       "object",
                "properties": props,
                "required":   required,
            },
        },
    }


# ─── Registre central ─────────────────────────────────────────────────────────
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolMeta] = {}

    def register(self, meta: ToolMeta):
        self._tools[meta.name] = meta

    def get_schemas(self) -> list[dict]:
        """Retourner les schémas JSON pour l'API Ollama."""
        return [m.schema for m in self._tools.values()]

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, name: str, params: dict) -> dict:
        """Exécuter un outil et retourner le résultat."""
        meta = self._tools.get(name)
        if not meta:
            return {"error": f"Outil inconnu: {name}. Disponibles: {self.list_tools()}"}
        try:
            if asyncio.iscoroutinefunction(meta.fn):
                result = await meta.fn(**params)
            else:
                result = await asyncio.to_thread(meta.fn, **params)
            return {"result": result, "tool": name}
        except TypeError as e:
            return {"error": f"Paramètres invalides pour {name}: {e}"}
        except Exception as e:
            return {"error": f"Erreur dans {name}: {e}", "trace": traceback.format_exc()[-500:]}

    async def execute_parallel(self, calls: list[dict]) -> list[dict]:
        """Exécuter plusieurs outils en parallèle (comme picoclaw loop.go)."""
        tasks = [self.execute(c["name"], c.get("arguments", {})) for c in calls]
        return await asyncio.gather(*tasks)


# ─── Instance globale ─────────────────────────────────────────────────────────
registry = ToolRegistry()
