"""Écriture des métadonnées, via mutagen.

C'est la voie d'appariement principale du serveur : readFileTags() lit
`albumartist` (à défaut `artist`), `album`, le n° de piste et le titre, puis
matchAlbum()/matchTrack() les rapprochent de la collection. Des tags corrects
valent mieux qu'un chemin correct — le chemin n'est qu'un repli.
"""

from __future__ import annotations

from pathlib import Path

from .disc import DiscInfo, TrackInfo


class TaggingError(RuntimeError):
    """Les métadonnées n'ont pas pu être écrites."""


def tag_file(path: Path, disc: DiscInfo, track: TrackInfo) -> None:
    """Écrit les tags sur un fichier déjà encodé.

    Le format est déduit de l'extension par mutagen, qui choisit le conteneur
    (Vorbis comments pour FLAC/Opus, ID3 pour MP3) et fait la correspondance
    des noms de champs.
    """
    try:
        import mutagen
    except ImportError as exc:
        raise TaggingError(
            "Le module « mutagen » est absent. Installe-le avec :\n"
            '  sudo dnf install python3-mutagen   (ou : pip install mutagen)'
        ) from exc

    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:
        raise TaggingError(f'Ouverture de {path.name} impossible : {exc}') from exc

    if audio is None:
        raise TaggingError(f'Format non reconnu par mutagen : {path.name}')

    if audio.tags is None:
        audio.add_tags()

    for key, value in _build_tags(disc, track).items():
        audio.tags[key] = value

    try:
        audio.save()
    except Exception as exc:
        raise TaggingError(f'Écriture des tags sur {path.name} : {exc}') from exc


def _build_tags(disc: DiscInfo, track: TrackInfo) -> dict[str, list[str]]:
    """Construit le jeu de tags, en clés « easy » communes aux formats."""
    album_artist = disc.album_artist()

    tags: dict[str, list[str]] = {
        'album': [disc.album],
        'title': [track.title],
        # L'artiste de piste diffère de celui de l'album sur une compilation ;
        # ailleurs les deux coïncident.
        'artist': [track.artist or disc.artist],
        'albumartist': [album_artist],
        'tracknumber': [str(track.number)],
    }

    if disc.tracks:
        tags['tracknumber'] = [f'{track.number}/{len(disc.tracks)}']

    if disc.disc_total > 1:
        tags['discnumber'] = [f'{disc.disc_number}/{disc.disc_total}']

    if disc.year:
        tags['date'] = [str(disc.year)]
    if disc.genre:
        tags['genre'] = [disc.genre]
    if disc.label:
        tags['organization'] = [disc.label]

    # Permet de retrouver l'édition exacte plus tard (re-tag, vérification).
    if disc.musicbrainz_release_id:
        tags['musicbrainz_albumid'] = [disc.musicbrainz_release_id]

    return tags
