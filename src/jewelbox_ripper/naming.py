"""Construction des chemins de sortie, alignée sur le scanner du serveur.

Le serveur (audioScanner.js / audioMatch.js) apparie les fichiers aux albums
déjà présents en base. Deux voies d'appariement, dans cet ordre :

1. les tags du fichier (albumartist/artist + album, puis n° de piste + titre) ;
2. à défaut, un repli sur le chemin « Artiste/Album/03 - Titre.flac ».

On vise donc les deux : tags corrects *et* arborescence conforme, pour que
l'appariement tienne même si la lecture des tags échoue côté serveur.
"""

from __future__ import annotations

import re
import unicodedata

# Interdits sous Linux (/ et NUL) plus les caractères qui gênent sur les
# partages réseau (SMB/CIFS) — un montage Samba est le cas d'usage typique
# quand le serveur tourne sur une autre machine.
_FORBIDDEN = r'[/\x00<>:"\\|?*]'

# Caractères de contrôle : invisibles, et fatals pour un nom de fichier.
_CONTROL = r'[\x01-\x1f\x7f]'

# Noms réservés Windows, hostiles sur un partage réseau monté depuis Linux.
_RESERVED = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}

MAX_COMPONENT = 255  # limite ext4/btrfs, en octets une fois encodé en UTF-8


def sanitize_component(name: str, *, fallback: str = 'Sans titre') -> str:
    """Rend une chaîne utilisable comme nom de dossier ou de fichier.

    Les accents sont conservés : la normalisation du serveur les gère
    (`normalize()` fait un NFD puis retire les diacritiques), donc « Björk »
    s'apparie sans souci et reste lisible dans un explorateur de fichiers.
    """
    if not name:
        return fallback

    # NFC : « é » composé plutôt que « e » + accent combinant, pour éviter
    # deux dossiers d'apparence identique sur des systèmes différents.
    cleaned = unicodedata.normalize('NFC', str(name))
    cleaned = re.sub(_CONTROL, '', cleaned)
    cleaned = re.sub(_FORBIDDEN, '-', cleaned)

    # Espaces multiples resserrés, et on retire les points/espaces de fin :
    # un composant terminé par « . » est refusé par SMB.
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = cleaned.rstrip('. ')

    # Un nom commençant par « . » serait ignoré par walkAudioFiles().
    cleaned = cleaned.lstrip('.').strip()

    if not cleaned:
        return fallback

    if cleaned.split('.')[0].upper() in _RESERVED:
        cleaned = f'_{cleaned}'

    return _truncate_utf8(cleaned, MAX_COMPONENT) or fallback


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Tronque sans jamais couper au milieu d'un caractère multi-octets."""
    encoded = value.encode('utf-8')
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode('utf-8', errors='ignore').rstrip('. ')


def track_filename(position: int | None, title: str, extension: str) -> str:
    """« 03 - Titre.flac » — le format que parsePathFallback() sait relire.

    Le serveur lit le n° via /^(\\d{1,3})[\\s.\\-_]*(.*)$/, donc deux chiffres
    minimum et un séparateur « - » restent dans les clous tout en gardant un
    tri alphabétique correct dans un explorateur.
    """
    ext = extension if extension.startswith('.') else f'.{extension}'
    safe_title = sanitize_component(title, fallback='Piste')

    if position is None:
        return f'{safe_title}{ext}'

    # Trois chiffres au-delà de 99 pistes : la regex du serveur en accepte
    # jusqu'à trois, et les coffrets dépassent parfois la centaine.
    width = 3 if position > 99 else 2
    stem = f'{position:0{width}d} - {safe_title}'

    # La troncature porte sur le titre seul, pour préserver le numéro et
    # l'extension qui portent l'appariement.
    budget = MAX_COMPONENT - len(ext.encode('utf-8'))
    return f'{_truncate_utf8(stem, budget)}{ext}'


def album_relpath(artist: str, album: str, *, disc: int | None = None,
                  disc_total: int | None = None) -> str:
    """Chemin relatif du dossier d'album, sous la racine de la bibliothèque.

    Pour un coffret, on ajoute un sous-dossier « CD1 », « CD2 »… :
    listFolderAudioFiles() descend d'exactement un niveau, donc cette
    disposition reste appariable via un dossier associé manuellement.
    """
    parts = [sanitize_component(artist, fallback='Artiste inconnu'),
             sanitize_component(album, fallback='Album inconnu')]

    # Pas de sous-dossier pour un disque unique : il ajouterait un niveau
    # inutile et casserait le repli « Artiste/Album/piste » du scanner.
    if disc is not None and disc_total is not None and disc_total > 1:
        parts.append(f'CD{disc}')

    return '/'.join(parts)
