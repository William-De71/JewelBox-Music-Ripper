"""Normalisation du volume : calcul des tags ReplayGain d'un album rippé.

Deux CD n'ont pas été masterisés au même niveau : sans correction, passer de
l'un à l'autre fait sursauter. rsgain mesure le volume perçu (EBU R128) et
écrit les tags ReplayGain — gain par piste ET par album — dans les fichiers ;
le lecteur JewelBox les applique ensuite (Préférences → Lecture). Les
échantillons audio ne sont jamais modifiés, seuls des tags sont ajoutés.

L'étape est facultative : sans rsgain installé, ou en cas d'échec, l'album
est publié quand même, simplement sans ces tags.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

RSGAIN = 'rsgain'
INSTALL_HINT = 'sudo dnf install rsgain'


@dataclass(frozen=True)
class ReplayGainResult:
    applied: bool
    # Explication à afficher quand rien n'a été écrit.
    message: str = ''


def replaygain_command(binary: str, files: list[Path]) -> list[str]:
    """rsgain en mode « custom » : -a calcule aussi le gain d'album (tous les
    fichiers passés forment l'album), -s i écrit les tags, -q fait taire la
    progression (le ripper affiche la sienne)."""
    return [binary, 'custom', '-a', '-s', 'i', '-q', *map(str, files)]


def apply_replaygain(files: list[Path], *, binary: str | None = None,
                     run: Callable = subprocess.run) -> ReplayGainResult:
    if not files:
        return ReplayGainResult(False, 'aucun fichier')
    binary = binary or shutil.which(RSGAIN)
    if not binary:
        return ReplayGainResult(
            False, f'rsgain absent — tags ReplayGain non écrits '
                   f'({INSTALL_HINT})')
    try:
        completed = run(replaygain_command(binary, files),
                        capture_output=True, text=True, check=False)
    except OSError as exc:
        return ReplayGainResult(False, f'rsgain : {exc}')
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or '').strip()
        last = detail.splitlines()[-1] if detail else f'code {completed.returncode}'
        return ReplayGainResult(False, f'rsgain a échoué : {last}')
    return ReplayGainResult(True)
