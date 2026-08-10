"""Extraction audio (cdparanoia) puis encodage vers le format demandé.

cdparanoia lit le CD avec correction d'erreurs : il relit les secteurs
douteux plutôt que d'accepter des clics. C'est plus lent qu'une copie brute,
et c'est précisément l'intérêt pour de l'archivage.

Chaque piste est extraite en WAV dans un dossier temporaire, encodée, puis le
WAV est supprimé — on ne garde jamais plus d'un intermédiaire à la fois.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .disc import TrackInfo


class RipError(RuntimeError):
    """Échec d'extraction ou d'encodage."""


class MissingToolError(RipError):
    """Un binaire externe requis est absent."""


@dataclass(frozen=True)
class EncoderSpec:
    """Comment produire un format donné à partir d'un WAV."""

    name: str
    extension: str
    binary: str
    lossless: bool
    install_hint: str

    def command(self, wav: Path, output: Path, quality: str | None) -> list[str]:
        if self.name == 'flac':
            # -8 : compression maximale. Sans perte, donc seuls la taille et
            # le temps CPU varient — jamais la qualité.
            return [self.binary, f'-{quality or "8"}', '--silent',
                    '--force', '-o', str(output), str(wav)]

        if self.name == 'opus':
            # Opus raisonne en débit cible ; 192k est transparent en stéréo.
            return [self.binary, '--quiet', '--bitrate', quality or '192',
                    str(wav), str(output)]

        if self.name == 'mp3':
            # -V : VBR piloté par la qualité, 0 = meilleur.
            return [self.binary, '--quiet', '-V', quality or '0',
                    str(wav), str(output)]

        raise RipError(f'Encodeur inconnu : {self.name}')


ENCODERS: dict[str, EncoderSpec] = {
    'flac': EncoderSpec('flac', '.flac', 'flac', True,
                        'sudo dnf install flac'),
    'opus': EncoderSpec('opus', '.opus', 'opusenc', False,
                        'sudo dnf install opus-tools'),
    'mp3': EncoderSpec('mp3', '.mp3', 'lame', False,
                       'sudo dnf install lame'),
}

# Le paranoia de Fedora s'appelle cdparanoia ; d'autres distributions
# fournissent cd-paranoia. On accepte les deux.
CDPARANOIA_BINARIES = ('cdparanoia', 'cd-paranoia')

ProgressCallback = Callable[[int, int, str], None]


def find_cdparanoia() -> str:
    for name in CDPARANOIA_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    raise MissingToolError(
        'cdparanoia est introuvable. Installe-le avec :\n'
        '  sudo dnf install cdparanoia'
    )


def resolve_encoder(fmt: str) -> EncoderSpec:
    """Valide le format demandé et vérifie que son encodeur est disponible."""
    spec = ENCODERS.get(fmt.lower())
    if spec is None:
        known = ', '.join(sorted(ENCODERS))
        raise RipError(f'Format « {fmt} » non pris en charge (connus : {known})')

    if not shutil.which(spec.binary):
        raise MissingToolError(
            f'{spec.binary} est introuvable, requis pour encoder en '
            f'{spec.name.upper()}. Installe-le avec :\n  {spec.install_hint}'
        )
    return spec


def rip_track(track_number: int, destination: Path, *,
              device: str | None = None,
              paranoia: bool = True) -> Path:
    """Extrait une piste en WAV. Renvoie le chemin du fichier produit."""
    binary = find_cdparanoia()
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [binary, '--quiet']
    if device:
        command += ['--force-cdrom-device', device]
    if not paranoia:
        # Désactive la vérification : bien plus rapide, mais accepte les
        # erreurs de lecture. Réservé aux disques en parfait état.
        command.append('--disable-paranoia')

    command += [str(track_number), str(destination)]

    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False)
    except OSError as exc:
        raise RipError(f'Lancement de cdparanoia impossible : {exc}') from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise RipError(
            f'Extraction de la piste {track_number} échouée '
            f'(code {result.returncode}) : {detail}'
        )

    if not destination.exists() or destination.stat().st_size == 0:
        raise RipError(
            f'Extraction de la piste {track_number} : fichier vide ou absent'
        )

    return destination


def encode(wav: Path, output: Path, spec: EncoderSpec,
           quality: str | None = None) -> Path:
    """Encode un WAV vers le format cible. Renvoie le chemin produit."""
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(spec.command(wav, output, quality),
                                capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RipError(f'Lancement de {spec.binary} impossible : {exc}') from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise RipError(f'Encodage {spec.name.upper()} échoué : {detail}')

    if not output.exists() or output.stat().st_size == 0:
        raise RipError(f'Encodage {spec.name.upper()} : fichier vide ou absent')

    return output


def rip_and_encode(track: TrackInfo, output: Path, spec: EncoderSpec, *,
                   workdir: Path,
                   device: str | None = None,
                   paranoia: bool = True,
                   quality: str | None = None) -> Path:
    """Extrait puis encode une piste, en nettoyant le WAV intermédiaire."""
    wav = workdir / f'track{track.number:03d}.wav'
    try:
        rip_track(track.number, wav, device=device, paranoia=paranoia)
        return encode(wav, output, spec, quality)
    finally:
        wav.unlink(missing_ok=True)


def eject(device: str | None = None) -> bool:
    """Éjecte le disque. Purement cosmétique : un échec n'est pas bloquant."""
    binary = shutil.which('eject')
    if not binary:
        return False
    command = [binary] + ([device] if device else [])
    try:
        return subprocess.run(command, capture_output=True,
                              check=False).returncode == 0
    except OSError:
        return False
