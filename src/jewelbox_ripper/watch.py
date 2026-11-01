"""Surveillance du lecteur : extraction lancée à l'insertion d'un CD.

`jewelbox-rip --watch` tourne en service utilisateur systemd. Il interroge le
lecteur toutes les deux secondes et, quand un CD audio vient d'être inséré,
ouvre un terminal avec `jewelbox-rip` interactif : on y choisit l'édition
(ou on passe par Discogs, la saisie manuelle), et on suit l'avancement et
les éventuelles erreurs directement.

Un disque déjà présent au démarrage n'est pas extrait : seul un disque inséré
pendant la surveillance déclenche quelque chose, sans quoi chaque ouverture
de session relancerait l'extraction du CD resté dans le lecteur.

Tous les accès au système (ioctl, sous-processus, notifications) sont
injectables : la logique reste testable sans lecteur optique.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_DEVICE = '/dev/sr0'
POLL_SECONDS = 2.0

# linux/cdrom.h
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDS_DISC_OK = 4
CDS_AUDIO = 100
CDS_MIXED = 105

# Terminaux essayés dans l'ordre, avec l'option qui précède la commande.
# xdg-terminal-exec respecte le terminal choisi par l'utilisateur ; Ptyxis est
# celui de GNOME sur Fedora récent.
TERMINALS = (
    ('xdg-terminal-exec', []),
    ('ptyxis', ['--']),
    ('gnome-terminal', ['--']),
    ('kgx', ['--']),
    ('konsole', ['-e']),
    ('xterm', ['-e']),
)


def drive_has_audio_disc(device: str) -> bool:
    """Vrai si le lecteur contient un disque prêt, audio (ou mixte).

    O_NONBLOCK : sans lui, ouvrir un lecteur vide fermerait le tiroir sur
    certains modèles, ou bloquerait jusqu'à ce qu'un disque soit prêt.
    """
    import fcntl

    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0) != CDS_DISC_OK:
            return False
        return fcntl.ioctl(fd, CDROM_DISC_STATUS, 0) in (CDS_AUDIO, CDS_MIXED)
    except OSError:
        return False
    finally:
        os.close(fd)


@dataclass
class InsertionDetector:
    """Transforme des relevés successifs en événements « disque inséré »."""

    present: bool | None = None

    def update(self, has_disc: bool) -> bool:
        # Premier relevé : on apprend l'état sans réagir (disque déjà là).
        inserted = self.present is False and has_disc
        self.present = has_disc
        return inserted


def rip_command(device: str) -> list[str]:
    """La commande d'extraction, avec le même interpréteur que le service."""
    return [sys.executable, '-m', 'jewelbox_ripper', '--device', device]


def terminal_command(command: list[str],
                     which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """La commande à lancer dans un terminal, qui reste ouvert à la fin.

    Sans le `read`, la fenêtre se fermerait sur le bilan avant qu'on le lise.
    """
    script = ' '.join(_quote(part) for part in command)
    script += "; echo; read -r -p 'Entrée pour fermer…' _"
    for name, prefix in TERMINALS:
        binary = which(name)
        if binary:
            return [binary, *prefix, 'bash', '-c', script]
    return None


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def notify(title: str, body: str = '',
           which: Callable[[str], str | None] = shutil.which,
           run: Callable[..., object] = subprocess.run) -> None:
    """Notification de bureau via notify-send ; toujours aussi dans le journal."""
    print(f'{title} {body}'.rstrip(), flush=True)
    binary = which('notify-send')
    if not binary:
        return
    try:
        run([binary, '--app-name=JewelBox Ripper', '--icon=media-optical',
             title, body], check=False)
    except OSError:
        pass


@dataclass
class Watcher:
    """Ouvre un terminal d'extraction pour chaque disque inséré."""

    device: str
    has_disc: Callable[[str], bool] = drive_has_audio_disc
    popen: Callable[..., object] = subprocess.Popen
    which: Callable[[str], str | None] = shutil.which
    notify: Callable[[str, str], None] = notify
    detector: InsertionDetector = field(default_factory=InsertionDetector)

    def poll(self) -> None:
        if self.detector.update(self.has_disc(self.device)):
            self.on_inserted()

    def on_inserted(self) -> None:
        command = terminal_command(rip_command(self.device), self.which)
        if command is None:
            self.notify('CD inséré',
                        'Aucun terminal trouvé : lance « jewelbox-rip » à la main.')
            return
        self.notify('CD inséré', "Extraction à suivre dans le terminal.")
        try:
            # Non bloquant : le terminal vit sa vie, la surveillance continue.
            self.popen(command, start_new_session=True)
        except OSError as exc:
            self.notify('CD inséré',
                        f'Terminal impossible à ouvrir ({exc}) : lance « jewelbox-rip ».')


def watch(device: str | None, *, sleep: Callable[[float], None] = time.sleep,
          watcher: Watcher | None = None,
          keep_going: Callable[[], bool] = lambda: True) -> int:
    """Boucle de surveillance ; ne rend la main qu'à l'arrêt du service."""
    watcher = watcher or Watcher(device or DEFAULT_DEVICE)
    print(f'Surveillance de {watcher.device} : insère un CD audio.', flush=True)
    while keep_going():
        watcher.poll()
        sleep(POLL_SECONDS)
    return 0
