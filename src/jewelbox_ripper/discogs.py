"""Recherche d'un disque dans la base Discogs, en repli de MusicBrainz.

Différence fondamentale avec `identify` : **Discogs n'indexe pas les Disc ID**.
Les identifiants dérivés de la table des matières d'un CD ne sont pas stockés
dans sa base, et aucune route ne permet de les interroger. Il n'existe donc
aucun moyen de retrouver automatiquement le disque inséré — la recherche part
forcément de termes saisis par l'utilisateur (artiste et album).

Ce repli reste rentable : plutôt que de retaper douze titres à la main, on
choisit une release et la tracklist arrive complète, avec année, label et
pochette.

La recherche exige un token (60 requêtes/minute contre 25 sans, et la route
`/database/search` est fermée aux requêtes anonymes). Sans token configuré, le
repli s'annonce indisponible et la saisie manuelle reprend la main.

Volontairement sur urllib, comme `api` : aucune dépendance HTTP à installer.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .disc import DiscInfo, TrackInfo

BASE_URL = 'https://api.discogs.com'
DEFAULT_TIMEOUT = 15.0

# Discogs impose un User-Agent identifiant, sous peine de bridage.
USER_AGENT = 'JewelBoxMusicRipper/1.0 +https://github.com/William-De71/JewelBox-Music-Ripper'

# Au-delà, le choix devient illisible dans un terminal.
MAX_RESULTS = 10


class DiscogsError(RuntimeError):
    """La recherche Discogs n'a pas abouti."""


class DiscogsAuthError(DiscogsError):
    """Token absent, invalide ou révoqué."""


@dataclass
class DiscogsRelease:
    """Une release Discogs, à départager par l'utilisateur.

    `release_id` sert à récupérer la tracklist : la recherche ne la renvoie
    pas, il faut un second appel sur /releases/{id}.
    """

    release_id: int
    title: str
    artist: str
    year: int | None
    country: str | None
    label: str | None
    catno: str | None
    formats: str | None
    track_count: int | None
    thumb: str | None
    raw: dict[str, Any]

    def summary(self) -> str:
        """Ligne descriptive pour le choix interactif."""
        bits = [self.artist, '—', self.title]

        details = [str(d) for d in (self.year, self.label, self.country) if d]
        if self.catno:
            details.append(self.catno)
        if details:
            bits.append(f"({', '.join(details)})")

        if self.formats:
            bits.append(f'[{self.formats}]')
        if self.track_count:
            bits.append(f'· {self.track_count} pistes')
        return ' '.join(bits)


class DiscogsClient:
    """Accès en lecture à la base Discogs."""

    def __init__(self, token: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not token:
            raise DiscogsAuthError('Token Discogs absent.')
        self.token = token
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _request(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f'{BASE_URL}{path}'
        if params:
            url = f'{url}?{urllib.parse.urlencode(params)}'

        request = urllib.request.Request(url, headers={
            'Accept': 'application/json',
            'User-Agent': USER_AGENT,
            # Forme attendue par Discogs Auth pour un jeton personnel.
            'Authorization': f'Discogs token={self.token}',
        })

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8')
                return json.loads(raw) if raw else None

        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise DiscogsAuthError(
                    'Token Discogs refusé. Vérifie « discogs_token » dans ta '
                    'configuration, ou la variable DISCOGS_TOKEN.\n'
                    'Un jeton se crée sur https://www.discogs.com/settings/developers'
                ) from exc
            if exc.code == 429:
                raise DiscogsError(
                    'Quota Discogs dépassé (60 requêtes/minute). '
                    'Patiente une minute avant de réessayer.'
                ) from exc
            raise DiscogsError(f'Discogs → HTTP {exc.code} : {exc.reason}') from exc

        except urllib.error.URLError as exc:
            raise DiscogsError(f'Discogs est injoignable : {exc.reason}') from exc

        except json.JSONDecodeError as exc:
            raise DiscogsError('Réponse illisible de Discogs.') from exc

    # -- recherche ---------------------------------------------------------

    def search(self, artist: str, album: str, *,
               track_count: int | None = None) -> list[DiscogsRelease]:
        """Cherche des releases par artiste et titre.

        `track_count` ne filtre pas la requête (Discogs ne le permet pas) : il
        sert à remonter les éditions au bon nombre de pistes en tête de liste,
        celles qui correspondent le plus probablement au disque inséré.
        """
        params = {'type': 'release', 'per_page': str(MAX_RESULTS)}
        if artist:
            params['artist'] = artist
        if album:
            params['release_title'] = album

        if not artist and not album:
            raise DiscogsError('Indique au moins un artiste ou un album.')

        payload = self._request('/database/search', params)
        results = (payload or {}).get('results') or []

        releases = [_to_release(item) for item in results[:MAX_RESULTS]]

        if track_count:
            # Tri stable : les éditions au bon nombre de pistes d'abord, le
            # classement par pertinence de Discogs est conservé au sein de
            # chaque groupe.
            releases.sort(key=lambda r: r.track_count != track_count)

        return releases

    def fetch_tracklist(self, release_id: int) -> dict[str, Any]:
        """Détail complet d'une release — la recherche ne rend pas les pistes."""
        payload = self._request(f'/releases/{release_id}')
        if not isinstance(payload, dict):
            raise DiscogsError(f'Release {release_id} illisible.')
        return payload


# -- conversion ----------------------------------------------------------


def _to_release(item: dict[str, Any]) -> DiscogsRelease:
    """Convertit un résultat de recherche.

    Discogs concatène artiste et titre dans « title » (« Nirvana - Nevermind »)
    et ne renvoie pas de champ artiste séparé sur cette route.
    """
    raw_title = str(item.get('title') or '')
    artist, _, title = raw_title.partition(' - ')
    if not title:
        artist, title = '', raw_title

    return DiscogsRelease(
        release_id=int(item.get('id') or 0),
        title=title.strip() or 'Album inconnu',
        artist=artist.strip() or 'Artiste inconnu',
        year=_parse_year(item.get('year')),
        country=item.get('country') or None,
        label=_first(item.get('label')),
        catno=item.get('catno') or None,
        formats=_join_formats(item.get('format')),
        track_count=_search_track_count(item),
        thumb=item.get('cover_image') or item.get('thumb') or None,
        raw=item,
    )


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None


def _join_formats(value: Any) -> str | None:
    """« CD, Album, Reissue » — utile pour écarter un vinyle ou un coffret."""
    if isinstance(value, list):
        parts = [str(v) for v in value if v]
        return ', '.join(parts) if parts else None
    return str(value) if value else None


def _search_track_count(item: dict[str, Any]) -> int | None:
    """Nombre de pistes annoncé par la recherche, quand il l'est.

    Discogs code le nombre de supports dans le format (« 2×CD ») mais pas le
    nombre de pistes : seul le détail de la release le donne vraiment.
    """
    for key in ('num_tracks', 'track_count'):
        value = item.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def build_disc_info(release: dict[str, Any], *,
                    fallback_track_count: int | None = None) -> DiscInfo:
    """Convertit une release Discogs détaillée en DiscInfo exploitable.

    Les positions Discogs sont des chaînes libres (« 1 », « A1 », « 2-3 ») : la
    numérotation est reconstruite séquentiellement, puisque cdparanoia extrait
    les pistes dans l'ordre physique du disque.
    """
    tracks: list[TrackInfo] = []

    for entry in release.get('tracklist') or []:
        # Les en-têtes de section (« type_ »: « heading ») ne sont pas des
        # pistes audio et fausseraient la numérotation.
        if entry.get('type_') not in (None, '', 'track'):
            continue

        title = str(entry.get('title') or '').strip()
        if not title:
            continue

        tracks.append(TrackInfo(
            number=len(tracks) + 1,
            title=title,
            duration_seconds=_parse_duration(entry.get('duration')),
            artist=_join_artists(entry.get('artists')) or None,
        ))

    artists = release.get('artists') or []
    artist = _join_artists(artists) or 'Artiste inconnu'

    labels = release.get('labels') or []
    label = None
    if labels:
        label = (labels[0] or {}).get('name') or None

    return DiscInfo(
        artist=artist,
        album=str(release.get('title') or 'Album inconnu'),
        tracks=tracks,
        year=_parse_year(release.get('year')),
        genre=_first(release.get('genres')),
        label=label,
        barcode=_extract_barcode(release),
        discogs_release_id=int(release.get('id')) if release.get('id') else None,
        discogs_cover_url=_extract_cover(release),
        is_compilation=_looks_like_compilation(artist, tracks),
    )


def _join_artists(artists: Any) -> str:
    """Reconstruit « A feat. B » depuis la structure Discogs.

    Chaque artiste porte le mot de liaison qui le suit dans « join ». Le suffixe
    numérique de désambiguïsation (« Nirvana (2) ») est retiré : il identifie
    l'artiste dans la base, il n'appartient pas à son nom.
    """
    if not isinstance(artists, list):
        return ''

    parts: list[str] = []
    for index, entry in enumerate(artists):
        if not isinstance(entry, dict):
            continue
        name = _strip_disambiguation(str(entry.get('name') or ''))
        if not name:
            continue
        parts.append(name)

        join = str(entry.get('join') or '').strip()
        if join and index < len(artists) - 1:
            # « , » se colle au mot précédent, les autres liaisons s'espacent.
            parts.append(join if join == ',' else f' {join} ')

    return ''.join(parts).replace('  ', ' ').strip().strip(',').strip()


def _strip_disambiguation(name: str) -> str:
    """« Nirvana (2) » → « Nirvana »."""
    return re.sub(r'\s*\(\d+\)$', '', name).strip()


def _parse_duration(value: Any) -> float | None:
    """« 4:38 » ou « 1:02:11 » → secondes. Chaîne vide très fréquente."""
    text = str(value or '').strip()
    if not text:
        return None

    parts = text.split(':')
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None

    if len(numbers) == 2:
        minutes, seconds = numbers
        return float(minutes * 60 + seconds)
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return float(hours * 3600 + minutes * 60 + seconds)
    return None


def _parse_year(value: Any) -> int | None:
    """L'année arrive en entier, en chaîne, ou en date « 1991-09-24 »."""
    text = str(value or '').strip()
    if not text:
        return None
    try:
        year = int(text[:4])
    except ValueError:
        return None
    return year if 1900 <= year <= 2100 else None


def _extract_barcode(release: dict[str, Any]) -> str | None:
    """Cherche le code-barres parmi les identifiants de la release."""
    for identifier in release.get('identifiers') or []:
        if not isinstance(identifier, dict):
            continue
        if str(identifier.get('type') or '').lower() == 'barcode':
            value = str(identifier.get('value') or '').strip()
            if value:
                # Les codes-barres Discogs sont souvent saisis avec des espaces
                # ou des tirets, alors que le serveur attend un EAN compact.
                compact = value.replace(' ', '').replace('-', '')
                return compact or None
    return None


def _extract_cover(release: dict[str, Any]) -> str | None:
    """URL de la pochette principale.

    Le serveur reconnaît le domaine Discogs et télécharge l'image lui-même à la
    création de l'album, exactement comme pour Cover Art Archive.
    """
    for image in release.get('images') or []:
        if not isinstance(image, dict):
            continue
        if image.get('type') == 'primary':
            url = image.get('uri') or image.get('resource_url')
            if url:
                return str(url)

    # Pas de pochette marquée « primary » : la première fait l'affaire.
    for image in release.get('images') or []:
        if isinstance(image, dict):
            url = image.get('uri') or image.get('resource_url')
            if url:
                return str(url)
    return None


def _looks_like_compilation(artist: str, tracks: list[TrackInfo]) -> bool:
    """Même règle que pour MusicBrainz : artiste générique, ou pistes variées."""
    if artist.strip().lower() in {'various', 'various artists', 'artistes divers'}:
        return True

    named = {t.artist for t in tracks if t.artist}
    return len(named) > 1 and len(named) >= max(2, len(tracks) // 2)
