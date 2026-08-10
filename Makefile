# Raccourcis de développement — `make help` pour la liste.

CONFIG_DIR ?= $(HOME)/.config/jewelbox-music-ripper
CONFIG     := $(CONFIG_DIR)/config.toml

.PHONY: help install uninstall config test rip dry-run

help:
	@echo "Cibles disponibles :"
	@echo "  make install   - installe la commande « jewelbox-rip » (mode editable)"
	@echo "  make config    - copie config.example.toml vers ~/.config (ne l'ecrase pas)"
	@echo "  make dry-run   - identifie le CD insere sans rien ecrire"
	@echo "  make rip       - extrait le CD insere"
	@echo "  make test      - lance les tests"
	@echo "  make uninstall - desinstalle la commande"

# Mode editable : les modifications du code sont prises en compte sans
# reinstaller. ~/.local/bin est deja dans le PATH.
install:
	pip install --user -e .
	@echo
	@echo "Commande « jewelbox-rip » installee."
	@command -v jewelbox-rip >/dev/null \
	  || echo "  ! Ajoute ~/.local/bin a ton PATH."
	@test -f "$(CONFIG)" \
	  || echo "  Etape suivante : make config"

uninstall:
	pip uninstall -y jewelbox-music-ripper

# Ne remplace jamais une configuration existante.
config:
	@test -f "$(CONFIG)" \
	  && echo "Configuration deja presente : $(CONFIG)" \
	  || ( mkdir -p "$(CONFIG_DIR)" \
	       && cp config.example.toml "$(CONFIG)" \
	       && echo "Cree : $(CONFIG)" \
	       && echo "  Renseigne « library_path » avant le premier rip." )

# Sans installation : utilise les sources locales.
dry-run:
	PYTHONPATH=src python3 -m jewelbox_ripper --dry-run

rip:
	PYTHONPATH=src python3 -m jewelbox_ripper

test:
	PYTHONPATH=src python3 -m pytest tests/ -q --no-cov
