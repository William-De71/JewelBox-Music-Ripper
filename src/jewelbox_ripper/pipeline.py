"""Orchestration : extraire un disque entier, puis le publier d'un bloc.

Règle centrale : le serveur ne doit jamais voir un album à moitié écrit. Tout
est extrait, encodé et tagué dans un dossier temporaire, et seul le dossier
complet est déplacé dans la bibliothèque — un rip interrompu ne laisse donc
rien derrière lui.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .disc import DiscInfo
from .loudness import ReplayGainResult, apply_replaygain
from .naming import album_relpath, track_filename
from .ripper import EncoderSpec, rip_and_encode
from .tagging import tag_file

# (index 1-based, total, libellé de l'étape en cours)
ProgressCallback = Callable[[int, int, str], None]


class PipelineError(RuntimeError):
    """Le rip n'a pas pu aboutir."""


@dataclass
class RipResult:
    """Bilan d'un rip terminé."""

    album_dir: Path
    files: list[Path] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    # None : étape désactivée (replaygain = false).
    replaygain: ReplayGainResult | None = None

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.failed


def plan_paths(disc: DiscInfo, config: Config) -> tuple[Path, list[tuple[int, Path]]]:
    """Calcule les chemins de sortie sans rien écrire — sert aussi au dry-run."""
    spec_ext = {'flac': '.flac', 'opus': '.opus', 'mp3': '.mp3'}
    extension = spec_ext.get(config.audio_format, f'.{config.audio_format}')

    rel_dir = album_relpath(
        disc.album_artist(), disc.album,
        disc=disc.disc_number, disc_total=disc.disc_total,
    )
    album_dir = config.library_path / rel_dir

    planned = [
        (track.number, album_dir / track_filename(track.number, track.title, extension))
        for track in disc.tracks
    ]
    return album_dir, planned


def rip_disc(disc: DiscInfo, config: Config, spec: EncoderSpec, *,
             progress: ProgressCallback | None = None) -> RipResult:
    """Extrait le disque entier et le publie dans la bibliothèque."""
    if not disc.tracks:
        raise PipelineError("Aucune piste à extraire.")

    album_dir, planned = plan_paths(disc, config)

    if album_dir.exists() and any(album_dir.iterdir()):
        raise PipelineError(
            f'Le dossier existe déjà et n\'est pas vide :\n  {album_dir}\n'
            'Supprime-le ou renomme-le avant de relancer, pour éviter de '
            'mélanger deux versions du même album.'
        )

    # Le temporaire est créé à côté de la bibliothèque : le déplacement final
    # reste ainsi sur le même système de fichiers, donc atomique. Un /tmp sur
    # un autre volume forcerait une copie, plus lente et interruptible.
    staging_parent = config.library_path / '.jewelbox-music-ripper-tmp'
    staging_parent.mkdir(parents=True, exist_ok=True)

    result = RipResult(album_dir=album_dir)
    total = len(disc.tracks)

    try:
        with tempfile.TemporaryDirectory(dir=staging_parent) as tmp:
            staging = Path(tmp)
            staged_files: list[Path] = []

            for index, (track, (_, final_path)) in enumerate(
                    zip(disc.tracks, planned), start=1):
                if progress:
                    progress(index, total, f'Piste {track.number} — {track.title}')

                staged = staging / final_path.name
                try:
                    rip_and_encode(
                        track, staged, spec,
                        workdir=staging,
                        device=config.device,
                        paranoia=config.paranoia,
                        quality=config.quality,
                    )
                    tag_file(staged, disc, track)
                    staged_files.append(staged)
                except Exception as exc:
                    # Une piste rayée ne doit pas condamner le reste du disque ;
                    # l'échec est reporté dans le bilan final.
                    result.failed.append((track.number, str(exc)))

            if not staged_files:
                raise PipelineError(
                    'Aucune piste n\'a pu être extraite. Vérifie le disque et '
                    'le lecteur.'
                )

            # Avant la publication : le serveur ne voit l'album qu'une fois
            # tagué en entier. Un échec ici n'empêche pas de publier.
            if config.replaygain:
                if progress:
                    progress(total, total, 'Calcul du volume (ReplayGain)')
                result.replaygain = apply_replaygain(staged_files)

            if progress:
                progress(total, total, 'Publication dans la bibliothèque')

            album_dir.mkdir(parents=True, exist_ok=True)
            for staged in staged_files:
                destination = album_dir / staged.name
                shutil.move(str(staged), str(destination))
                result.files.append(destination)

    finally:
        # Ne disparaît que s'il est vide : un temporaire d'un rip concurrent
        # doit survivre.
        try:
            staging_parent.rmdir()
        except OSError:
            pass

    return result
