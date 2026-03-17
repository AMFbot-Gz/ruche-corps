# La Ruche — Agent IA Autonome Souverain

Agent IA autonome sur macOS avec:
- **Nemotron-3-Super** (230B) comme modèle principal via Ollama
- **32+ outils** : computer use, shell, web, code, mémoire vectorielle
- **Worker de nuit** : missions HTN tâche par tâche, même sans surveillance
- **Mémoire vectorielle** : ChromaDB + embeddings sémantiques
- **Goals loop** : objectifs autonomes générés par l'agent lui-même
- **Watchdog** : auto-réparation des services
- **Telegram** : contrôle et rapports depuis mobile

## Démarrage
```bash
cp .env.example .env  # configurer les tokens
./start.sh            # lancer tout
./start.sh --goals    # avec boucle d'objectifs autonomes
```

## Architecture
```
ruche-corps/
├── agent.py          # Agent principal (ReAct loop, 15 iter max)
├── worker.py         # Worker autonome missions longues
├── goals.py          # Boucle d'objectifs autonomes (SQLite)
├── watchdog.py       # Surveillance + auto-réparation
├── memory.py         # Mémoire vectorielle (ChromaDB)
├── router.py         # Routeur de messages (model selection)
├── core/             # Fiabilité: structlog, Pydantic, circuit breaker
├── tools/            # 32+ outils @tool
├── computer/         # Computer use macOS (screenshot, click, type)
├── missions/         # HTN planner + executor + queue
├── senses/           # Telegram + Voice
└── context/          # Context builder 128K tokens
```
