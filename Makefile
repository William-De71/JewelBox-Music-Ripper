# Raccourcis de développement — `make help` pour la liste.

CONFIG_DIR ?= $(HOME)/.config/jewelbox-music-ripper
CONFIG     := $(CONFIG_DIR)/config.toml

# Source de « make update » : le dépôt distant, jamais le dossier local. La
# commande installée est ainsi indépendante de l'état du clone de dev.
GIT_URL  ?= https://github.com/William-De71/JewelBox-Music-Ripper.git
REPO_URL ?= git+$(GIT_URL)

# Dernier tag publié, résolu sur le dépôt distant. « = » et non « := » : la
# résolution n'a lieu que dans la recette de update, pas à chaque appel de make.
# --sort=-v:refname trie par numéro de version, donc v1.0.10 passe après v1.0.9.
LATEST_TAG = $(shell git ls-remote --tags --refs --sort=-v:refname \
                       "$(GIT_URL)" 'v*' 2>/dev/null \
                     | head -1 | sed 's#.*refs/tags/##')

# Seules les versions taguées sont installées : « main » peut contenir du code
# non publié. Pour une version precise : make update REF=v1.0.0
REF ?= $(LATEST_TAG)

PKG := jewelbox-music-ripper

.PHONY: help require-pipx install update uninstall config test build clean rip dry-run

help:
	@echo "Cibles disponibles :"
	@echo "  make install   - installe la commande « jewelbox-rip » (venv isole)"
	@echo "  make update    - met a jour vers la derniere version publiee"
	@echo "  make config    - copie config.example.toml vers ~/.config (ne l'ecrase pas)"
	@echo "  make dry-run   - identifie le CD insere sans rien ecrire (sources locales)"
	@echo "  make rip       - extrait le CD insere (sources locales)"
	@echo "  make test      - tests + gate de couverture 95 % (comme la CI)"
	@echo "  make build     - construit le wheel et le sdist dans dist/"
	@echo "  make clean     - supprime les artefacts de build et de tests"
	@echo "  make uninstall - desinstalle la commande"

# pipx est le prerequis commun a install/update/uninstall.
require-pipx:
	@command -v pipx >/dev/null || { \
	  echo "pipx est absent. Installe-le :"; \
	  echo "  sudo dnf install pipx"; \
	  echo "  pipx ensurepath   # une fois, pour ~/.local/bin dans le PATH"; \
	  exit 1; }

# Installe $(1) sans jamais laisser la machine sans commande utilisable.
#
# « pipx install --force » ne convient pas : avec le backend uv, il rappelle
# « uv venv » sans --clear sur un venv deja present, ce qui echoue. Desinstaller
# d'abord reglerait ce cas, mais laisserait la commande absente si l'install
# suivante echoue (depot injoignable, version inexistante, build casse).
#
# On installe donc d'abord sous un nom suffixe — venv et lanceur distincts, donc
# aucun contact avec l'installation en place — et on ne bascule qu'une fois le
# nouveau lanceur verifie. En cas d'echec a n'importe quelle etape, le staging
# est retire et l'ancienne version reste en service.
#
# La fenetre sans commande se limite au uninstall/install final, ou le paquet
# est deja construit et en cache : la seule vraie cause d'echec restante serait
# une panne disque.
STAGE := $(PKG)-staging
define pipx-swap
	pipx uninstall $(STAGE) >/dev/null 2>&1 || true; \
	trap 'pipx uninstall $(STAGE) >/dev/null 2>&1 || true' EXIT; \
	echo "Installation de controle..."; \
	pipx install --suffix=-staging $(1) || exit 1; \
	jewelbox-rip-staging --version >/dev/null 2>&1 || { \
	  echo "La nouvelle version ne demarre pas. Version en place conservee."; \
	  exit 1; }; \
	echo "Bascule..."; \
	pipx uninstall $(PKG) >/dev/null 2>&1 || true; \
	pipx install $(1) || { \
	  echo "Echec de la bascule. Reprise : make install"; \
	  exit 1; }
endef

# pipx copie le code dans un venv dedie (~/.local/share/pipx/venvs) et pose le
# lanceur dans ~/.local/bin. Aucun lien vers ce dossier de dev : le deplacer,
# changer de branche ou faire « make clean » n'affecte pas la commande
# installee. Les dependances restent isolees du site-packages utilisateur.
install: require-pipx
	@$(call pipx-swap,.)
	@echo
	@echo "Commande « jewelbox-rip » installee (copie isolee du code)."
	@command -v jewelbox-rip >/dev/null \
	  || echo "  ! Ajoute ~/.local/bin a ton PATH : pipx ensurepath"
	@test -f "$(CONFIG)" \
	  || echo "  Etape suivante : make config"

# Reinstalle la derniere version taguee depuis le depot distant, sans toucher
# au clone local ni a la configuration. Pour une autre version, ou pour revenir
# en arriere : make update REF=v1.0.0
update: require-pipx
	@ref="$(REF)"; \
	 test -n "$$ref" || { \
	   echo "Impossible de determiner la derniere version publiee."; \
	   echo "Verifie ta connexion, ou indique-la : make update REF=v1.0.1"; \
	   exit 1; }; \
	 echo "Version ciblee : $$ref"; \
	 $(call pipx-swap,"$(REPO_URL)@$$ref"); \
	 echo; \
	 echo "Mise a jour terminee ($$ref)."
	@jewelbox-rip --version 2>/dev/null || true

uninstall: require-pipx
	pipx uninstall jewelbox-music-ripper
	@echo "La configuration est conservee : $(CONFIG)"

# Ne remplace jamais une configuration existante.
config:
	@test -f "$(CONFIG)" \
	  && echo "Configuration deja presente : $(CONFIG)" \
	  || ( mkdir -p "$(CONFIG_DIR)" \
	       && cp config.example.toml "$(CONFIG)" \
	       && echo "Cree : $(CONFIG)" \
	       && echo "  Renseigne « library_path » avant le premier rip." )

# Sans installation : utilise les sources locales, sans toucher a la commande
# installee. C'est la voie a suivre pour tester une modification.
dry-run:
	PYTHONPATH=src python3 -m jewelbox_ripper --dry-run

rip:
	PYTHONPATH=src python3 -m jewelbox_ripper

# Identique a la gate appliquee par la CI sur chaque PR (min 95 %).
test:
	python3 -m pytest

# Meme chaine que le job de release : wheel + sdist, puis controle des metadonnees.
build: clean
	python3 -m build
	python3 -m twine check dist/*

clean:
	rm -rf dist build htmlcov .coverage coverage.xml
	rm -rf src/*.egg-info .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
