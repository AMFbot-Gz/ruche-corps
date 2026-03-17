#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# start.sh — Lancer La Ruche complète en une commande
# Usage: ./start.sh [--goals] [--no-voice] [--help]
# ═══════════════════════════════════════════════════════════════

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
DIM='\033[2m'
RESET='\033[0m'

# Aide
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    echo "Usage: ./start.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --goals      Lancer aussi le Goals Loop"
    echo "  --no-voice   Désactiver la synthèse vocale"
    echo "  --help       Afficher cette aide"
    exit 0
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo ""
echo "╔═══════════════════════════════════════════╗"
echo "║        LA RUCHE — DÉMARRAGE               ║"
echo "╚═══════════════════════════════════════════╝"
echo ""

# Créer les répertoires nécessaires
mkdir -p ~/.ruche/logs ~/.ruche/reports ~/.ruche/memory/chroma

# Tableau de bord final (rempli au fil des étapes)
declare -A STATUS_MAP

# ─── 1. Ollama ────────────────────────────────────────────────
if ! curl -s --max-time 3 http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Ollama absent — tentative de lancement...${RESET}"
    open -a Ollama 2>/dev/null || ollama serve > /dev/null 2>&1 &
    sleep 3
fi

if curl -s --max-time 3 http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Ollama actif${RESET}"
    STATUS_MAP["Ollama"]="✅ actif"
else
    echo -e "${YELLOW}⚠️  Ollama ne répond pas${RESET}"
    STATUS_MAP["Ollama"]="⚠️  absent"
fi

# ─── 2. Redis (via Docker) ───────────────────────────────────
REDIS_OK=false
if redis-cli -p 6379 ping > /dev/null 2>&1; then
    REDIS_OK=true
fi

if [[ "$REDIS_OK" == false ]]; then
    if command -v docker &>/dev/null; then
        echo -e "${BLUE}▶ Lancement Redis Docker...${RESET}"
        docker start ruche-redis > /dev/null 2>&1 || \
        docker run -d --name ruche-redis -p 6379:6379 --restart unless-stopped redis:alpine > /dev/null 2>&1 || true
        sleep 2
        redis-cli -p 6379 ping > /dev/null 2>&1 && REDIS_OK=true
    fi
fi

if [[ "$REDIS_OK" == true ]]; then
    echo -e "${GREEN}✅ Redis actif${RESET}"
    STATUS_MAP["Redis"]="✅ localhost:6379"
else
    echo -e "${YELLOW}⚠️  Redis absent — continuer sans cache session${RESET}"
    STATUS_MAP["Redis"]="⚠️  absent"
fi

# ─── 3. Ghost OS (si présent) ────────────────────────────────
if [ -f ~/Projects/ghost-os-ultimate/src/queen_oss.js ]; then
    if ! curl -s --max-time 2 http://localhost:3000/api/health > /dev/null 2>&1; then
        echo -e "${BLUE}▶ Lancement Ghost OS...${RESET}"
        cd ~/Projects/ghost-os-ultimate
        nohup node src/queen_oss.js > ~/.ruche/logs/ghost_os.log 2>&1 &
        cd "$DIR"
        sleep 2
        if curl -s --max-time 2 http://localhost:3000/api/health > /dev/null 2>&1; then
            echo -e "${GREEN}✅ Ghost OS démarré${RESET}"
            STATUS_MAP["Ghost OS"]="✅ localhost:3000"
        else
            echo -e "${YELLOW}⚠️  Ghost OS démarré (vérifier logs)${RESET}"
            STATUS_MAP["Ghost OS"]="⚠️  démarré"
        fi
    else
        echo -e "${GREEN}✅ Ghost OS déjà actif${RESET}"
        STATUS_MAP["Ghost OS"]="✅ localhost:3000"
    fi
fi

# ─── 4. Worker autonome ──────────────────────────────────────
WORKER_PID_FILE="$DIR/.worker.pid"
if [ -f "$WORKER_PID_FILE" ] && kill -0 "$(cat "$WORKER_PID_FILE")" 2>/dev/null; then
    echo -e "${GREEN}✅ Worker déjà actif (PID $(cat "$WORKER_PID_FILE"))${RESET}"
    STATUS_MAP["Worker"]="✅ PID $(cat "$WORKER_PID_FILE")"
else
    echo -e "${BLUE}▶ Démarrage Worker autonome...${RESET}"
    PYTHONUNBUFFERED=1 python3 -u "$DIR/worker.py" \
        >> ~/.ruche/logs/worker.log 2>&1 &
    WORKER_PID=$!
    echo "$WORKER_PID" > "$WORKER_PID_FILE"
    echo -e "${GREEN}✅ Worker démarré (PID $WORKER_PID)${RESET} ${DIM}→ ~/.ruche/logs/worker.log${RESET}"
    STATUS_MAP["Worker"]="✅ PID $WORKER_PID"
fi

# ─── 4b. Watchdog ────────────────────────────────────────────
WATCHDOG_PID_FILE="$DIR/.watchdog.pid"
if [ -f "$WATCHDOG_PID_FILE" ] && kill -0 "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null; then
    echo -e "${GREEN}✅ Watchdog déjà actif (PID $(cat "$WATCHDOG_PID_FILE"))${RESET}"
    STATUS_MAP["Watchdog"]="✅ PID $(cat "$WATCHDOG_PID_FILE")"
else
    echo -e "${BLUE}▶ Démarrage Watchdog...${RESET}"
    PYTHONUNBUFFERED=1 python3 -u "$DIR/watchdog.py" >> ~/.ruche/logs/watchdog.log 2>&1 &
    WATCHDOG_PID=$!
    echo "$WATCHDOG_PID" > "$WATCHDOG_PID_FILE"
    echo -e "${GREEN}✅ Watchdog démarré (PID $WATCHDOG_PID)${RESET} ${DIM}→ ~/.ruche/logs/watchdog.log${RESET}"
    STATUS_MAP["Watchdog"]="✅ PID $WATCHDOG_PID"
fi

# ─── 4c. Goals Loop (si --goals) ────────────────────────────
GOALS_PID_FILE="$DIR/.goals.pid"
if [[ "$*" == *"--goals"* ]]; then
    if [ -f "$GOALS_PID_FILE" ] && kill -0 "$(cat "$GOALS_PID_FILE")" 2>/dev/null; then
        echo -e "${GREEN}✅ Goals Loop déjà actif (PID $(cat "$GOALS_PID_FILE"))${RESET}"
        STATUS_MAP["Goals"]="✅ PID $(cat "$GOALS_PID_FILE")"
    else
        echo -e "${BLUE}▶ Démarrage Goals Loop...${RESET}"
        PYTHONUNBUFFERED=1 python3 -u "$DIR/goals.py" >> ~/.ruche/logs/goals.log 2>&1 &
        GOALS_PID=$!
        echo "$GOALS_PID" > "$GOALS_PID_FILE"
        echo -e "${GREEN}✅ Goals Loop démarré (PID $GOALS_PID)${RESET} ${DIM}→ ~/.ruche/logs/goals.log${RESET}"
        STATUS_MAP["Goals"]="✅ PID $GOALS_PID"
    fi
else
    STATUS_MAP["Goals"]="⚪ non lancé (--goals pour activer)"
fi

# ─── 5. Agent principal ──────────────────────────────────────
echo ""
echo -e "${BLUE}▶ Démarrage La Ruche Agent...${RESET}"

# Tableau récapitulatif avant de passer la main à l'agent
echo ""
echo "╔═══════════════════════════════════════════╗"
echo "║         RÉSUMÉ DU DÉMARRAGE               ║"
echo "╠═══════════════════════════════════════════╣"
for key in "Ollama" "Redis" "Ghost OS" "Worker" "Watchdog" "Goals"; do
    if [[ -n "${STATUS_MAP[$key]+_}" ]]; then
        printf "║  %-12s  %-27s║\n" "$key" "${STATUS_MAP[$key]}"
    fi
done
echo "╚═══════════════════════════════════════════╝"
echo ""
echo -e "${DIM}Logs: ~/.ruche/logs/ | Arrêt: ./ruche stop${RESET}"
echo ""

# Lancer l'agent (exec remplace ce shell)
# Le PID sera celui du process python3 main.py
exec python3 "$DIR/main.py" "$@"
