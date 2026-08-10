"""Identification du disque via son Disc ID MusicBrainz.

Le Disc ID est calculé à partir de la table des matières (TOC) du CD : c'est
une empreinte du disque physique, bien plus fiable qu'une recherche par
titre. Un même album pressé deux fois donne deux Disc ID différents, ce qui
permet de retrouver l'édition exacte — et explique aussi qu'une requête
puisse renvoyer plusieurs releases candidates.

`discid` et `musicbrainzngs` sont importés paresseusement : la chaîne
complète reste testable sans lecteur optique ni accès réseau.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .disc import DiscInfo, TrackInfo

USER_AGENT = ('jewelbox-music-ripper', '0.1.0',
              'https://github.com/William-De71/jewelbox-music-library')


class IdentificationError(RuntimeError):
    """Le disque n'a pas pu être identifié."""


class DiscReadError(IdentificationError):
    """Le disque n'a pas pu être lu (pas de CD, lecteur absent, disque rayé)."""


@dataclass
class ReleaseCandidate:
    """Une édition possible du disque, à départager par l'utilisateur."""

    release_id: str
    title: str
    artist: str
    date: str | None
    country: str | None
    label: str | None
    barcode: str | None
    track_count: int
    disambiguation: str | None
    raw: dict[str, Any]

    def summary(self) -> str:
        """Ligne descriptive pour le choix interactif."""
        bits = [self.artist, '—', self.title]
        details = [d for d in (self.date, self.country, self.label) if d]
        if details:
            bits.append(f"({', '.join(details)})")
        if self.disambiguation:
            bits.append(f'[{self.disambiguation}]')
        bits.append(f'· {self.track_count} pistes')
        return ' '.join(bits)


def read_disc_id(device: str | None = None) -> tuple[str, int]:
    """Lit le TOC du disque inséré. Renvoie (disc_id, nombre de pistes)."""
    try:
        import discid
    except ImportError as exc:
        raise DiscReadError(
            "Le module « discid » est absent. Installe-le avec :\n"
            "  sudo dnf install python3-discid   (ou : pip install discid)"
        ) from exc

    try:
        disc = discid.read(device) if device else discid.read()
    except Exception as exc:  # discid.DiscError et cie
        raise DiscReadError(
            f'Lecture du disque impossible : {exc}\n'
            "Vérifie qu'un CD audio est inséré et que le lecteur est accessible."
        ) from exc

    return disc.id, len(disc.tracks)


def lookup_candidates(disc_id: str) -> list[ReleaseCandidate]:
    """Interroge MusicBrainz. Liste vide si le disque est inconnu."""
    try:
        import musicbrainzngs
    except ImportError as exc:
        raise IdentificationError(
            "Le module « musicbrainzngs » est absent. Installe-le avec :\n"
            "  pip install musicbrainzngs"
        ) from exc

    musicbrainzngs.set_useragent(*USER_AGENT)

    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc_id, includes=['artists', 'recordings', 'labels'])
    except musicbrainzngs.ResponseError:
        # 404 = disque inconnu du service : pas une erreur, juste un repli
        # vers la saisie manuelle.
        return []
    except musicbrainzngs.MusicBrainzError as exc:
        raise IdentificationError(
            f'MusicBrainz est injoignable : {exc}'
        ) from exc

    # Selon que le Disc ID est connu ou seulement le TOC, la réponse arrive
    # sous « disc » ou sous « cdstub »/« release-list ».
    releases = []
    if 'disc' in result:
        releases = result['disc'].get('release-list', [])
    elif 'release-list' in result:
        releases = result['release-list']

    return [_to_candidate(r) for r in releases]


def _to_candidate(release: dict[str, Any]) -> ReleaseCandidate:
    artist = _join_artist_credit(release.get('artist-credit', []))

    label = None
    label_info = release.get('label-info-list') or []
    if label_info:
        label = (label_info[0].get('label') or {}).get('name')

    return ReleaseCandidate(
        release_id=release.get('id', ''),
        title=release.get('title', 'Album inconnu'),
        artist=artist or 'Artiste inconnu',
        date=release.get('date'),
        country=release.get('country'),
        label=label,
        barcode=release.get('barcode') or None,
        track_count=_count_tracks(release),
        disambiguation=release.get('disambiguation') or None,
        raw=release,
    )


def _join_artist_credit(credit: list[Any]) -> str:
    """Reconstruit « A feat. B » depuis la structure MusicBrainz.

    Le crédit alterne dictionnaires d'artistes et chaînes de liaison
    (« feat. », « & »), qu'il faut conserver pour un rendu fidèle.
    """
    parts: list[str] = []
    for item in credit:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if 'artist' in item:
                parts.append(item['artist'].get('name', ''))
            if item.get('joinphrase'):
                parts.append(item['joinphrase'])
    return ''.join(parts).strip()


def _count_tracks(release: dict[str, Any]) -> int:
    for medium in release.get('medium-list', []):
        count = medium.get('track-count')
        if count:
            return int(count)
    return 0


def build_disc_info(candidate: ReleaseCandidate,
                    *, disc_id: str | None = None) -> DiscInfo:
    """Convertit une release MusicBrainz en DiscInfo exploitable."""
    release = candidate.raw
    medium, disc_number, disc_total = _select_medium(release, disc_id)

    tracks: list[TrackInfo] = []
    for entry in (medium or {}).get('track-list', []):
        recording = entry.get('recording', {})

        # La durée est en millisecondes ; celle de la piste prime sur celle
        # de l'enregistrement (elle tient compte de l'édition).
        length = entry.get('length') or recording.get('length')
        duration = float(length) / 1000.0 if length else None

        track_artist = _join_artist_credit(
            recording.get('artist-credit', [])) or None

        tracks.append(TrackInfo(
            number=int(entry.get('position') or len(tracks) + 1),
            title=recording.get('title') or entry.get('title') or 'Sans titre',
            duration_seconds=duration,
            artist=track_artist,
        ))

    tracks.sort(key=lambda t: t.number)

    return DiscInfo(
        artist=candidate.artist,
        album=candidate.title,
        tracks=tracks,
        year=_parse_year(candidate.date),
        label=candidate.label,
        barcode=candidate.barcode,
        musicbrainz_release_id=candidate.release_id or None,
        disc_number=disc_number,
        disc_total=disc_total,
        is_compilation=_looks_like_compilation(candidate.artist, tracks),
    )


def _select_medium(release: dict[str, Any],
                   disc_id: str | None) -> tuple[dict[str, Any] | None, int, int]:
    """Retrouve le support correspondant au disque inséré.

    Sur un coffret, seul le CD physiquement présent doit être extrait : on
    l'identifie par son Disc ID plutôt que de prendre le premier support.
    """
    media = release.get('medium-list', []) or []
    total = len(media) or 1

    if disc_id:
        for medium in media:
            for disc in medium.get('disc-list', []) or []:
                if disc.get('id') == disc_id:
                    return medium, int(medium.get('position') or 1), total

    if media:
        return media[0], int(media[0].get('position') or 1), total
    return None, 1, 1


def _parse_year(date: str | None) -> int | None:
    if not date:
        return None
    try:
        return int(str(date)[:4])
    except ValueError:
        return None


def _looks_like_compilation(artist: str, tracks: list[TrackInfo]) -> bool:
    """Détecte une compilation : artiste générique, ou pistes hétérogènes."""
    if artist.strip().lower() in {'various artists', 'various', 'artistes divers'}:
        return True

    named = {t.artist for t in tracks if t.artist}
    return len(named) > 1 and len(named) >= max(2, len(tracks) // 2)
