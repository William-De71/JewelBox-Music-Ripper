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
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .disc import TrackInfo


class RipError(RuntimeError):
    """Échec d'extraction ou d'encodage."""


class MissingToolError(RipError):
    """Un binaire externe requis est absent."""


class RipTimeoutError(RipError):
    """cdparanoia a dépassé le temps accordé à la piste."""


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

# cdparanoia n'a aucune limite de temps : sur une zone difficile, il peut relire
# les mêmes secteurs pendant des heures sans rien écrire. Un disque sain s'extrait
# bien plus vite que le temps réel, donc deux fois la durée de la piste ne pénalise
# que les zones qui posent vraiment problème.
RIP_BUDGET_FACTOR = 2
RIP_BUDGET_MIN_SECONDS = 300.0
# Durée inconnue (saisie manuelle, métadonnées incomplètes).
RIP_BUDGET_DEFAULT_SECONDS = 1800.0

ProgressCallback = Callable[[int, int, str], None]

# (avancement de 0 à 1, ou None si la durée est inconnue ; étape en cours)
TrackProgressCallback = Callable[[float | None, str], None]

PHASE_READ = 'Lecture'
PHASE_FAST_READ = 'Relecture rapide'
PHASE_ENCODE = 'Encodage'

# Audio CD : 44,1 kHz, 16 bits, stéréo. L'en-tête WAV fait 44 octets.
WAV_BYTES_PER_SECOND = 176_400
WAV_HEADER_BYTES = 44
PROGRESS_POLL_SECONDS = 0.5


def wav_fraction(size: int, duration_seconds: float | None) -> float | None:
    """Part de la piste déjà écrite dans le WAV. None si la durée est inconnue."""
    if not duration_seconds:
        return None
    audio_bytes = max(0, size - WAV_HEADER_BYTES)
    return min(1.0, audio_bytes / (duration_seconds * WAV_BYTES_PER_SECOND))


@contextmanager
def _report_wav_growth(wav: Path, duration_seconds: float | None, phase: str,
                       report: TrackProgressCallback | None) -> Iterator[None]:
    """Rapporte l'avancement d'après la taille du WAV pendant son écriture.

    Le rapport continue quand le fichier ne grandit plus : c'est ce qui permet
    de voir le temps passer pendant que cdparanoia relit une zone difficile.
    """
    if report is None:
        yield
        return

    def current_fraction() -> float | None:
        try:
            size = wav.stat().st_size
        except OSError:
            size = 0
        return wav_fraction(size, duration_seconds)

    def poll() -> None:
        while not stop.wait(PROGRESS_POLL_SECONDS):
            report(current_fraction(), phase)

    stop = threading.Event()
    report(wav_fraction(0, duration_seconds), phase)
    watcher = threading.Thread(target=poll, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join()


def rip_budget(duration_seconds: float | None) -> float:
    """Temps maximal accordé à une lecture de piste, en secondes."""
    if not duration_seconds:
        return RIP_BUDGET_DEFAULT_SECONDS
    return max(RIP_BUDGET_MIN_SECONDS, duration_seconds * RIP_BUDGET_FACTOR)


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
              paranoia: bool = True,
              timeout: float | None = None) -> Path:
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
                                check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RipTimeoutError(
            f'Extraction de la piste {track_number} interrompue après '
            f'{int(exc.timeout) // 60} min : lecture trop lente'
        ) from exc
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
                   quality: str | None = None,
                   on_degraded: Callable[[], None] | None = None,
                   on_progress: TrackProgressCallback | None = None) -> Path:
    """Extrait puis encode une piste, en nettoyant le WAV intermédiaire.

    Si la lecture vérifiée dépasse son budget, la piste est relue sans
    vérification. `on_degraded` signale alors à l'appelant une piste produite,
    mais dont l'exactitude n'est pas garantie.
    """
    wav = workdir / f'track{track.number:03d}.wav'
    budget = rip_budget(track.duration_seconds)
    duration = track.duration_seconds
    degraded = False
    try:
        try:
            with _report_wav_growth(wav, duration, PHASE_READ, on_progress):
                rip_track(track.number, wav, device=device, paranoia=paranoia,
                          timeout=budget)
        except RipTimeoutError:
            if not paranoia:
                raise
            wav.unlink(missing_ok=True)
            with _report_wav_growth(wav, duration, PHASE_FAST_READ, on_progress):
                rip_track(track.number, wav, device=device, paranoia=False,
                          timeout=budget)
            degraded = True

        if on_progress:
            on_progress(1.0, PHASE_ENCODE)
        encoded = encode(wav, output, spec, quality)
        if degraded and on_degraded:
            on_degraded()
        return encoded
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
