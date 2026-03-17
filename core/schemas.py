"""
core/schemas.py — Modèles Pydantic v2 pour La Ruche

Définit et valide TOUT ce qui entre dans le système.
Utilise la validation stricte Pydantic v2 pour éviter les données malformées.
"""
from pydantic import BaseModel, Field


class InboundMessage(BaseModel):
    """Message entrant depuis n'importe quel canal (Telegram, CLI, Redis, etc.)"""
    channel:    str
    user_id:    str
    text:       str
    session_id: str = "unknown"
    source:     str = "unknown"


class ToolCall(BaseModel):
    """Appel d'outil demandé par le LLM"""
    name:      str
    arguments: dict = {}


class TaskResult(BaseModel):
    """Résultat d'une tâche exécutée par l'agent"""
    success:    bool
    content:    str
    tool_calls: list[ToolCall] = []
    error:      str | None = None


class MissionPayload(BaseModel):
    """Mission envoyée à l'agent via la queue Redis"""
    mission:  str
    priority: int = Field(default=3, ge=1, le=5)
    source:   str = "agent"
