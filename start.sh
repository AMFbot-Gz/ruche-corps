#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# start.sh — Lancer La Ruche complète en une commande
# ═══════════════════════════════════════════════════════════════
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "╔═══════════════════════════════════════════╗"
echo "║        LA RUCHE — DÉMARRAGE               ║"
echo "╚═══════════════════════════════════════════╝"

# 1. Ollama
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
  echo "⚠️  Ollama absent — tentative de lancement..."
  open -a Ollama 2>/dev/null || ollama serve &
  sleep 3
fi
echo "✅ Ollama actif"

# 2. Redis (via Docker)
if ! redis-cli -p 6379 ping > /dev/null 2>&1; then
  if command -v docker &>/dev/null; then
    echo "▶ Lancement Redis Docker..."
    docker run -d --name ruche-redis -p 6379:6379 redis:alpine 2>/dev/null || \
    docker start ruche-redis 2>/dev/null || true
    sleep 2
  fi
fi
redis-cli ping > /dev/null 2>&1 && echo "✅ Redis actif" || echo "⚠️  Redis absent — continuer sans cache session"

# 3. Ghost OS (si présent)
if [ -f ~/Projects/ghost-os-ultimate/src/queen_oss.js ]; then
  if ! curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
    echo "▶ Lancement Ghost OS..."
    cd ~/Projects/ghost-os-ultimate
    nohup node src/queen_oss.js > ~/.ruche/logs/ghost_os.log 2>&1 &
    cd "$DIR"
    sleep 2
    echo "✅ Ghost OS démarré"
  else
    echo "✅ Ghost OS déjà actif"
  fi
fi

# 4. Lancer l'agent
echo ""
echo "▶ Démarrage La Ruche Agent..."
exec python3 "$DIR/main.py" "$@"
