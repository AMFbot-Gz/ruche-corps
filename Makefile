.PHONY: start start-goals stop restart status logs health install mission models help

# Cible par défaut
.DEFAULT_GOAL := help

start:
	@./ruche start

start-goals:
	@./ruche start --goals

stop:
	@./ruche stop

restart:
	@./ruche restart

status:
	@./ruche status

# make logs            → logs agent en follow
# make logs worker     → logs worker en follow
logs:
	@./ruche logs $(filter-out $@,$(MAKECMDGOALS)) --follow

health:
	@./ruche health

install:
	@bash ./install.sh

mission:
	@./ruche mission "$(filter-out $@,$(MAKECMDGOALS))"

models:
	@./ruche models

help:
	@echo ""
	@echo "  La Ruche — Commandes disponibles"
	@echo "  ─────────────────────────────────"
	@echo "  make start        Lancer tous les services"
	@echo "  make start-goals  Lancer avec Goals Loop"
	@echo "  make stop         Arrêter tous les services"
	@echo "  make restart      Redémarrer"
	@echo "  make status       État des services"
	@echo "  make logs         Suivre les logs agent"
	@echo "  make logs worker  Suivre les logs worker"
	@echo "  make health       Check rapide de tous les services"
	@echo "  make install      Installation complète"
	@echo "  make models       Lister les modèles Ollama"
	@echo ""

# Absorber les arguments passés aux cibles (ex: make logs worker)
%:
	@:
