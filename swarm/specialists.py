"""
swarm/specialists.py — Les 5 agents spécialistes préconfigurés

Chaque spécialiste a:
- Un rôle précis
- Des outils limités à son domaine
- Un système prompt adapté
"""
from swarm.base import SpecialistAgent
from config import CFG

CODE_AGENT = SpecialistAgent(
    name="code_agent",
    role="Expert en développement logiciel. Tu édites, analyses et exécutes du code.",
    allowed_tools=[
        "shell", "run_python", "read_file", "write_file", "edit_file",
        "find_files", "list_dir", "code_edit", "analyze_code",
    ],
    model=CFG.M_CODE,   # qwen3-coder pour le code
    max_iter=10,
)

WEB_AGENT = SpecialistAgent(
    name="web_agent",
    role="Expert en recherche web. Tu trouves, récupères et analyses des informations en ligne.",
    allowed_tools=["web_search", "web_fetch"],
    model=CFG.M_GENERAL,
    max_iter=5,
)

FILE_AGENT = SpecialistAgent(
    name="file_agent",
    role="Expert en gestion de fichiers. Tu organises, lis, écris et cherches des fichiers.",
    allowed_tools=[
        "read_file", "write_file", "edit_file", "list_dir", "find_files",
        "load_context", "shell",
    ],
    model=CFG.M_FAST,
    max_iter=6,
)

MEMORY_AGENT = SpecialistAgent(
    name="memory_agent",
    role="Expert en mémoire. Tu mémorises, rappelles et résumes les informations importantes.",
    allowed_tools=[
        "remember", "recall", "summarize_session", "get_learned_rules", "add_rule",
    ],
    model=CFG.M_FAST,
    max_iter=4,
)

COMPUTER_AGENT = SpecialistAgent(
    name="computer_agent",
    role="Expert en contrôle macOS. Tu contrôles l'interface graphique avec précision.",
    allowed_tools=[
        "see_screen", "screenshot_region", "click", "double_click", "right_click",
        "drag_drop", "type_text", "hotkey", "key_press", "move_mouse", "scroll",
        "open_app", "applescript",
    ],
    model=CFG.M_GENERAL,
    max_iter=8,
)

# Dict pour accès par clé (utilisé par Queen et delegate_to_swarm)
SPECIALISTS = {
    "code":     CODE_AGENT,
    "web":      WEB_AGENT,
    "file":     FILE_AGENT,
    "memory":   MEMORY_AGENT,
    "computer": COMPUTER_AGENT,
}
