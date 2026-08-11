"""Modèle du disque à extraire, indépendant de sa provenance.

Un DiscInfo peut venir de MusicBrainz, d'une saisie manuelle ou d'un test :
le reste de la chaîne (rip, tags, nommage, API) ne connaît que ce type.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def format_duration(seconds: float | None) -> str | None:
    """Formate en « mm:ss » (ou « h:mm:ss »), comme le reste de la collection."""
    if seconds is None or seconds < 0:
        return None
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


@dataclass
class TrackInfo:
    """Une piste du disque. `number` est l'index physique lu par cdparanoia."""

    number: int
    title: str
    duration_seconds: float | None = None
    artist: str | None = None  # renseigné surtout sur les compilations

    @property
    def duration_label(self) -> str | None:
        return format_duration(self.duration_seconds)


@dataclass
class DiscInfo:
    """Un disque identifié, prêt à être extrait."""

    artist: str
    album: str
    tracks: list[TrackInfo] = field(default_factory=list)
    year: int | None = None
    genre: str | None = None
    label: str | None = None
    barcode: str | None = None
    musicbrainz_release_id: str | None = None
    discogs_release_id: int | None = None
    # Contrairement à Cover Art Archive, dont l'URL se déduit de l'identifiant
    # de release, Discogs donne l'adresse de l'image dans sa réponse.
    discogs_cover_url: str | None = None
    disc_number: int = 1
    disc_total: int = 1
    is_compilation: bool = False

    @property
    def total_duration(self) -> str | None:
        known = [t.duration_seconds for t in self.tracks
                 if t.duration_seconds is not None]
        if not known:
            return None
        return format_duration(sum(known))

    @property
    def cover_url(self) -> str | None:
        """URL de la pochette avant, selon la source qui a identifié le disque.

        On ne télécharge rien ici : le serveur reconnaît les URL
        coverartarchive.org comme celles de Discogs, et récupère l'image
        lui-même à la création de l'album (downloadCover, albums.js).

        MusicBrainz passe en premier : son URL est stable et sans quota, là où
        l'image Discogs est servie derrière un CDN.
        """
        if self.musicbrainz_release_id:
            return (f'https://coverartarchive.org/release/'
                    f'{self.musicbrainz_release_id}/front')
        return self.discogs_cover_url

    def album_artist(self) -> str:
        """L'artiste sous lequel classer l'album.

        Les compilations partent sous « Artistes divers » : c'est ainsi que
        le serveur regroupera l'album, plutôt que de l'éparpiller sur autant
        d'artistes qu'il y a de pistes.
        """
        return 'Artistes divers' if self.is_compilation else self.artist
