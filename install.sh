#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# install.sh — La Ruche — Installation complète
# Usage: ./install.sh
# Ou:    curl -fsSL https://raw.githubusercontent.com/AMFbot-Gz/ruche-corps/main/install.sh | bash
# ═══════════════════════════════════════════════════════════════

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
RESET='\033[0m'

step() { echo -e "\n${BOLD}${BLUE}▶ $1${RESET}"; }
ok()   { echo -e "${GREEN}  ✅ $1${RESET}"; }
warn() { echo -e "${YELLOW}  ⚠️  $1${RESET}"; }
fail() { echo -e "${RED}  ❌ $1${RESET}"; exit 1; }
info() { echo -e "${CYAN}  ℹ️  $1${RESET}"; }

# Mode dry-run / check
DRY_RUN=false
if [[ "$1" == "--check" || "$1" == "--dry-run" ]]; then
    DRY_RUN=true
    echo -e "${YELLOW}Mode --check: simulation sans installation${RESET}"
fi

# Aide
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    echo "Usage: ./install.sh [--check|--dry-run]"
    echo ""
    echo "  (aucun argument)   Installation complète"
    echo "  --check / --dry-run  Vérifie les prérequis sans installer"
    exit 0
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo ""
echo "╔═══════════════════════════════════════════╗"
echo "║     LA RUCHE — INSTALLATION               ║"
echo "║     Agent IA Autonome Souverain            ║"
echo "╚═══════════════════════════════════════════╝"
echo ""

# ═══════════════════════════════════════════════════════════════
# Étape 1: Prérequis
# ═══════════════════════════════════════════════════════════════
step "1/7 — Vérification des prérequis"

PREREQ_OK=true

# Python 3.10+
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    if [[ $PY_MAJOR -ge 3 && $PY_MINOR -ge 10 ]]; then
        ok "Python $PY_VER"
    else
        fail "Python >= 3.10 requis (actuel: $PY_VER)"
    fi
else
    fail "Python 3 non trouvé — installer depuis https://python.org"
fi

# pip3
if command -v pip3 &>/dev/null; then
    ok "pip3 disponible"
else
    warn "pip3 non trouvé — installation des dépendances échouera"
    PREREQ_OK=false
fi

# git
if command -v git &>/dev/null; then
    ok "git $(git --version | awk '{print $3}')"
else
    warn "git non trouvé"
fi

# Docker (optionnel)
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    ok "Docker disponible"
    HAS_DOCKER=true
else
    warn "Docker absent ou non démarré — Redis via Docker non disponible"
    HAS_DOCKER=false
fi

# redis-cli
if command -v redis-cli &>/dev/null; then
    ok "redis-cli disponible"
    HAS_REDIS_CLI=true
else
    warn "redis-cli non trouvé (optionnel)"
    HAS_REDIS_CLI=false
fi

# Ollama
if command -v ollama &>/dev/null; then
    ok "ollama disponible"
    HAS_OLLAMA=true
else
    warn "ollama non trouvé — télécharger depuis https://ollama.ai"
    HAS_OLLAMA=false
fi

if [[ "$DRY_RUN" == true ]]; then
    echo -e "\n${YELLOW}Mode --check terminé. Aucune modification effectuée.${RESET}"
    exit 0
fi

# ═══════════════════════════════════════════════════════════════
# Étape 2: Répertoires
# ═══════════════════════════════════════════════════════════════
step "2/7 — Création des répertoires"

RUCHE_DIR="$HOME/.ruche"
DIRS=(
    "$RUCHE_DIR/logs"
    "$RUCHE_DIR/reports"
    "$RUCHE_DIR/reports/reflections"
    "$RUCHE_DIR/images"
    "$RUCHE_DIR/memory/chroma"
    "$RUCHE_DIR/sessions"
    "$RUCHE_DIR/workspace"
)

for d in "${DIRS[@]}"; do
    mkdir -p "$d"
done
ok "Répertoires créés dans $RUCHE_DIR"

# ═══════════════════════════════════════════════════════════════
# Étape 3: Configuration .env
# ═══════════════════════════════════════════════════════════════
step "3/7 — Configuration"

ENV_FILE="$RUCHE_DIR/.env"
ENV_EXAMPLE="$DIR/.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$ENV_EXAMPLE" ]]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        ok "Fichier $ENV_FILE créé depuis .env.example"
        echo ""
        echo -e "${YELLOW}  ⚠️  Configure ton fichier .env:${RESET}"
        echo -e "  ${CYAN}  - TELEGRAM_BOT_TOKEN${RESET} (depuis @BotFather sur Telegram)"
        echo -e "  ${CYAN}  - TELEGRAM_ADMIN_ID${RESET} (ton user ID Telegram)"
        echo ""
        echo -e "  ${BOLD}Fichier à éditer: $ENV_FILE${RESET}"
        echo ""

        # Ouvrir dans l'éditeur par défaut (macOS: TextEdit, fallback: nano)
        if command -v open &>/dev/null; then
            echo -e "  ${BLUE}Ouverture dans l'éditeur...${RESET}"
            open -t "$ENV_FILE" 2>/dev/null || true
        fi

        read -r -p "  Appuie sur Entrée quand tu as fini de configurer .env..." _CONFIRM
    else
        warn ".env.example introuvable — création d'un .env minimal"
        cat > "$ENV_FILE" << 'EOF'
OLLAMA_HOST=http://localhost:11434
REDIS_URL=redis://localhost:6379
TG_TOKEN=your_telegram_token_here
TG_ADMIN=your_telegram_user_id_here
CHROMA_PATH=~/.ruche/chroma
LOG_LEVEL=INFO
OWNER=ton_nom
EOF
        ok "$ENV_FILE créé (à compléter)"
    fi
else
    ok "Configuration existante conservée ($ENV_FILE)"
fi

# ═══════════════════════════════════════════════════════════════
# Étape 4: Dépendances Python
# ═══════════════════════════════════════════════════════════════
step "4/7 — Dépendances Python"

if [[ -f "$DIR/requirements.txt" ]]; then
    echo -e "  ${BLUE}Installation en cours...${RESET}"
    # Essayer d'abord sans --break-system-packages (venvs, conda)
    if pip3 install -r "$DIR/requirements.txt" -q 2>/dev/null; then
        ok "Dépendances installées"
    elif pip3 install -r "$DIR/requirements.txt" --break-system-packages -q 2>/dev/null; then
        ok "Dépendances installées (--break-system-packages)"
    else
        warn "Installation partielle — certains modules peuvent manquer"
        echo -e "  ${CYAN}Lance manuellement: pip3 install -r requirements.txt${RESET}"
    fi

    # Vérification des imports critiques
    CRITICAL_IMPORTS=("redis" "httpx" "chromadb" "structlog" "pydantic")
    ALL_OK=true
    for mod in "${CRITICAL_IMPORTS[@]}"; do
        if python3 -c "import $mod" 2>/dev/null; then
            ok "import $mod"
        else
            warn "$mod non importable"
            ALL_OK=false
        fi
    done

    if [[ "$ALL_OK" == false ]]; then
        warn "Certains modules critiques manquent — l'agent peut ne pas démarrer"
    fi
else
    warn "requirements.txt introuvable dans $DIR"
fi

# ═══════════════════════════════════════════════════════════════
# Étape 5: Redis
# ═══════════════════════════════════════════════════════════════
step "5/7 — Redis"

REDIS_OK=false

# Test connexion directe
if [[ "$HAS_REDIS_CLI" == true ]] && redis-cli -p 6379 ping > /dev/null 2>&1; then
    ok "Redis déjà actif sur localhost:6379"
    REDIS_OK=true
elif python3 -c "import redis; redis.Redis().ping()" 2>/dev/null; then
    ok "Redis actif (vérifié via Python)"
    REDIS_OK=true
fi

# Démarrage via Docker si absent
if [[ "$REDIS_OK" == false ]]; then
    if [[ "$HAS_DOCKER" == true ]]; then
        echo -e "  ${BLUE}Tentative de démarrage Redis via Docker...${RESET}"
        # Essayer de redémarrer le conteneur existant
        if docker start ruche-redis > /dev/null 2>&1; then
            sleep 2
            ok "Redis redémarré (conteneur ruche-redis existant)"
            REDIS_OK=true
        elif docker run -d --name ruche-redis -p 6379:6379 --restart unless-stopped redis:alpine > /dev/null 2>&1; then
            sleep 2
            ok "Redis démarré via Docker (nouveau conteneur ruche-redis)"
            REDIS_OK=true
        else
            warn "Échec du démarrage Redis via Docker"
        fi
    fi
fi

if [[ "$REDIS_OK" == false ]]; then
    warn "Redis absent — certaines fonctions dégradées (pas de cache de session)"
    info "Pour installer Redis: brew install redis && brew services start redis"
fi

# ═══════════════════════════════════════════════════════════════
# Étape 6: Ollama + modèles
# ═══════════════════════════════════════════════════════════════
step "6/7 — Ollama et modèles"

OLLAMA_OK=false

if curl -s --max-time 3 http://localhost:11434/api/tags > /dev/null 2>&1; then
    ok "Ollama actif sur localhost:11434"
    OLLAMA_OK=true
    # Lister les modèles disponibles
    MODELS=$(curl -s --max-time 5 http://localhost:11434/api/tags | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = [m['name'] for m in data.get('models', [])]
    print('\n'.join(models))
except:
    pass
" 2>/dev/null)

    if [[ -n "$MODELS" ]]; then
        MODEL_COUNT=$(echo "$MODELS" | wc -l | tr -d ' ')
        ok "$MODEL_COUNT modèle(s) disponible(s)"
        echo "$MODELS" | while read -r m; do
            info "$m"
        done

        # Vérifier le modèle recommandé
        if echo "$MODELS" | grep -q "nemotron-3-super"; then
            ok "Modèle principal nemotron-3-super:cloud disponible"
        else
            warn "nemotron-3-super:cloud absent"
            info "Modèle minimal recommandé: ollama pull llama3.2:3b"
            read -r -p "  Télécharger llama3.2:3b maintenant? [y/N] " PULL_CONFIRM
            if [[ "$PULL_CONFIRM" =~ ^[Yy]$ ]]; then
                ollama pull llama3.2:3b
            fi
        fi
    else
        warn "Aucun modèle installé"
        info "Installer un modèle: ollama pull llama3.2:3b"
    fi
elif [[ "$HAS_OLLAMA" == true ]]; then
    warn "Ollama installé mais pas actif — tentative de lancement..."
    open -a Ollama 2>/dev/null || ollama serve > /dev/null 2>&1 &
    sleep 4
    if curl -s --max-time 3 http://localhost:11434/api/tags > /dev/null 2>&1; then
        ok "Ollama démarré"
        OLLAMA_OK=true
    else
        warn "Ollama ne répond pas encore — relance après démarrage"
    fi
else
    warn "Ollama absent — l'agent tournera en mode dégradé"
    info "Installer Ollama: https://ollama.ai/download"
fi

# ═══════════════════════════════════════════════════════════════
# Étape 7: Validation finale
# ═══════════════════════════════════════════════════════════════
step "7/7 — Validation"

# Rendre ruche exécutable
if [[ -f "$DIR/ruche" ]]; then
    chmod +x "$DIR/ruche"
    ok "CLI ruche rendu exécutable"
fi

# Test d'import de l'agent
TOOL_COUNT=$(python3 -c "
import sys
sys.path.insert(0, '$DIR')
try:
    from tools.registry import registry
    tools = registry.list_tools()
    print(len(tools))
except Exception as e:
    print(0)
" 2>/dev/null)

if [[ "$TOOL_COUNT" -gt 0 ]] 2>/dev/null; then
    ok "Agent importé avec succès — $TOOL_COUNT outils disponibles"
else
    warn "Import agent partiel (normal si dépendances manquantes)"
fi

# Résumé final
echo ""
echo "╔═══════════════════════════════════════════╗"
echo "║  ✅ Installation terminée!                ║"
echo "║                                           ║"
echo "║  Pour démarrer:  ./ruche start            ║"
echo "║  Pour arrêter:   ./ruche stop             ║"
echo "║  Pour le statut: ./ruche status           ║"
echo "║  Pour l'aide:    ./ruche --help           ║"
echo "╚═══════════════════════════════════════════╝"
echo ""
