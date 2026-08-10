#!/usr/bin/env bash
# Cut a release, npm-version style:
#   ./release.sh patch|minor|major   # bump from version in __init__.py
#   ./release.sh 0.3.0               # or set an explicit version
# Bumps __version__ in src/jewelbox_ripper/__init__.py (the single source of
# truth — pyproject.toml reads it dynamically), creates a commit titled "0.3.0"
# and the tag v0.3.0, then pushes main + tag. The tag triggers the Release
# workflow (.github/workflows/release.yml), which builds the wheel + sdist and
# publishes the GitHub Release.
#
# This version-bump commit is the one sanctioned direct-to-main commit (same
# convention as the JewelBox-Music-Desktop-Player and JewelBox-Music-Library
# repos): it makes each release visible in the commit history.
set -euo pipefail
cd "$(dirname "$0")"

INIT_FILE="src/jewelbox_ripper/__init__.py"

ARG="${1:?usage: ./release.sh patch|minor|major|X.Y.Z}"
[[ "$ARG" =~ ^(major|minor|patch|[0-9]+\.[0-9]+\.[0-9]+)$ ]] \
  || { echo "❌ Argument invalide : « $ARG » (attendu patch|minor|major ou X.Y.Z)"; exit 1; }

[[ "$(git branch --show-current)" == "main" ]] || { echo "❌ À lancer depuis main"; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "❌ Working tree non propre — committe ou stash d'abord"; exit 1; }
git pull --ff-only

# La gate de couverture avant toute chose : une release qui ne passe pas ses
# tests n'a pas à exister. La CI la rejouera, mais autant échouer localement.
echo "🧪 Tests + couverture…"
python3 -m pytest -q

# Resolve major/minor/patch against the current version (after the pull, so the
# base version is the freshest one from main).
if [[ "$ARG" =~ ^(major|minor|patch)$ ]]; then
  CUR="$(sed -n "s/^__version__ = '\([0-9]\+\.[0-9]\+\.[0-9]\+\)'.*/\1/p" "$INIT_FILE")"
  [[ "$CUR" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] \
    || { echo "❌ version introuvable ou invalide dans $INIT_FILE : « $CUR »"; exit 1; }
  MAJ="${BASH_REMATCH[1]}"; MIN="${BASH_REMATCH[2]}"; PAT="${BASH_REMATCH[3]}"
  case "$ARG" in
    major) V="$((MAJ + 1)).0.0" ;;
    minor) V="$MAJ.$((MIN + 1)).0" ;;
    patch) V="$MAJ.$MIN.$((PAT + 1))" ;;
  esac
  echo "ℹ️  $CUR → $V ($ARG)"
else
  V="$ARG"
fi

if git rev-parse "v$V" >/dev/null 2>&1; then
  echo "❌ Le tag v$V existe déjà"; exit 1
fi

# Bump the single source of truth. pyproject.toml picks it up via
# [tool.setuptools.dynamic], so there is nothing else to keep in sync.
sed -i "s/^__version__ = '[0-9]\+\.[0-9]\+\.[0-9]\+'/__version__ = '$V'/" "$INIT_FILE"
grep -q "__version__ = '$V'" "$INIT_FILE" \
  || { echo "❌ Échec du bump de version dans $INIT_FILE"; exit 1; }

# Vérifie que le paquet se construit bien avec la nouvelle version : mieux vaut
# le découvrir maintenant qu'après avoir poussé le tag.
echo "📦 Vérification du build…"
rm -rf dist
python3 -m build --wheel --outdir dist >/dev/null
ls dist/jewelbox_music_ripper-"$V"-py3-none-any.whl >/dev/null 2>&1 \
  || { echo "❌ Le wheel construit ne porte pas la version $V"; git checkout "$INIT_FILE"; exit 1; }
rm -rf dist

git add "$INIT_FILE"
git commit -m "$V"
git tag "v$V"
git push origin main "v$V"

echo "✅ Release v$V poussée — GitHub Actions construit et publie le paquet."
