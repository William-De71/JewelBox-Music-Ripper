"""Configuration : fichier TOML, surchargé par les options de ligne de commande.

Le chemin de la bibliothèque doit être fourni ici : le serveur le range en
base (réglage `music_library_path`) et ne l'expose par aucune route, donc le
ripper ne peut pas le découvrir tout seul.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV = 'JEWELBOX_RIPPER_CONFIG'
DISCOGS_TOKEN_ENV = 'DISCOGS_TOKEN'
DEFAULT_SERVER = 'http://localhost:3001'
DEFAULT_FORMAT = 'flac'


class ConfigError(RuntimeError):
    """Configuration absente, illisible ou incohérente."""


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()

    base = os.environ.get('XDG_CONFIG_HOME') or '~/.config'
    return Path(base).expanduser() / 'jewelbox-music-ripper' / 'config.toml'


@dataclass
class Config:
    """Réglages effectifs d'une exécution."""

    library_path: Path
    server_url: str = DEFAULT_SERVER
    audio_format: str = DEFAULT_FORMAT
    quality: str | None = None
    device: str | None = None
    paranoia: bool = True
    # Tags ReplayGain calculés après l'encodage (rsgain, si installé).
    replaygain: bool = True
    # Facultatif : sans lui, le repli Discogs est simplement indisponible.
    discogs_token: str | None = None

    def validate(self) -> None:
        """Vérifie la bibliothèque avant d'extraire quoi que ce soit.

        Mieux vaut refuser tout de suite que découvrir le problème après
        vingt minutes de rip.
        """
        if not self.library_path.exists():
            raise ConfigError(
                f'Bibliothèque introuvable : {self.library_path}\n'
                'Vérifie le chemin, et que le partage réseau est bien monté.'
            )
        if not self.library_path.is_dir():
            raise ConfigError(
                f"« {self.library_path} » n'est pas un dossier."
            )
        if not os.access(self.library_path, os.W_OK):
            raise ConfigError(
                f'Bibliothèque en lecture seule : {self.library_path}\n'
                "Le ripper doit pouvoir y écrire (monte le partage en "
                'écriture, ou corrige les droits).'
            )


def load_config(path: Path | None = None, *, required: bool = False) -> dict:
    """Charge le TOML. Renvoie un dict vide si le fichier est absent."""
    config_path = path or default_config_path()

    if not config_path.exists():
        if required:
            raise ConfigError(f'Fichier de configuration introuvable : {config_path}')
        return {}

    try:
        with open(config_path, 'rb') as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'TOML invalide dans {config_path} : {exc}') from exc
    except OSError as exc:
        raise ConfigError(f'Lecture de {config_path} impossible : {exc}') from exc


def build_config(file_data: dict, overrides: dict) -> Config:
    """Fusionne fichier et options CLI — la ligne de commande l'emporte."""
    def pick(key: str, default=None):
        value = overrides.get(key)
        return value if value is not None else file_data.get(key, default)

    library = pick('library_path')
    if not library:
        # Message actionnable : la première exécution se fait forcément sans
        # configuration, autant donner la commande exacte qui la crée.
        raise ConfigError(
            'Aucune bibliothèque configurée.\n\n'
            'Crée ta configuration une bonne fois pour toutes :\n'
            '  make config\n\n'
            f'puis renseigne « library_path » dans :\n'
            f'  {default_config_path()}\n\n'
            'Ce doit être le dossier scanné par le serveur (réglage « Chemin '
            'de la bibliothèque musicale »).\n'
            'Ponctuellement, tu peux aussi passer --library /chemin.'
        )

    paranoia = pick('paranoia', True)

    # La variable d'environnement l'emporte sur le fichier : elle permet de
    # garder le token hors du dossier de configuration. Une variable vide ou
    # réduite à des espaces ne doit pas masquer le token du fichier.
    token = (os.environ.get(DISCOGS_TOKEN_ENV) or '').strip() \
        or pick('discogs_token')

    return Config(
        library_path=Path(str(library)).expanduser(),
        server_url=str(pick('server_url', DEFAULT_SERVER)).rstrip('/'),
        audio_format=str(pick('audio_format', DEFAULT_FORMAT)).lower(),
        quality=(str(pick('quality')) if pick('quality') is not None else None),
        device=(str(pick('device')) if pick('device') else None),
        paranoia=bool(paranoia),
        replaygain=bool(pick('replaygain', True)),
        discogs_token=(str(token).strip() or None) if token else None,
    )
