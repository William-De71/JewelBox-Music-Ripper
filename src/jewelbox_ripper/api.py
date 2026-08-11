"""Client HTTP du serveur JewelBox Music Library.

Endpoints utilisés (vérifiés dans server/src/routes/, préfixe /api) :

  GET   /api/settings                          → réglages, dont Discogs
  GET   /api/albums/duplicate?title=&artist=   → { duplicate, album }
  POST  /api/albums                            → 201 album | 409 conflit
  PATCH /api/albums/:id                        → mise à jour (is_wanted…)
  POST  /api/player/scan                       → lance l'appariement
  GET   /api/player/scan/status                → progression et résultat

Volontairement sur urllib : le ripper n'a ainsi aucune dépendance HTTP à
installer, et le volume d'appels est trivial.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT = 15.0

# Le scan parcourt toute la bibliothèque : bien plus lent qu'un appel normal.
SCAN_TIMEOUT = 120.0


class ApiError(RuntimeError):
    """Échec d'un appel à l'API serveur."""

    def __init__(self, message: str, *, status: int | None = None,
                 payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class AlbumConflict(ApiError):
    """409 : l'album (ou son EAN) existe déjà côté serveur."""


@dataclass
class TrackPayload:
    """Une piste telle que POST /api/albums l'attend."""

    position: int
    title: str
    duration: str | None = None  # « mm:ss », comme le reste de la collection

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {'position': self.position, 'title': self.title}
        if self.duration:
            data['duration'] = self.duration
        return data


@dataclass
class AlbumPayload:
    """Un album tel que POST /api/albums l'attend.

    Seuls `title` et `artist_name` sont requis par le schéma Fastify ; tout
    champ vide est omis pour ne pas écraser des valeurs par du vide.
    """

    title: str
    artist_name: str
    year: int | None = None
    genre: str | None = None
    label_name: str | None = None
    total_duration: str | None = None
    ean: str | None = None
    cover_url: str | None = None
    notes: str | None = None
    tracks: list[TrackPayload] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            'title': self.title,
            'artist_name': self.artist_name,
        }
        for key in ('year', 'genre', 'label_name', 'total_duration',
                    'ean', 'cover_url', 'notes'):
            value = getattr(self, key)
            if value not in (None, ''):
                data[key] = value
        if self.tracks:
            data['tracks'] = [t.to_json() for t in self.tracks]
        return data


class JewelBoxApi:
    """Accès en lecture et écriture à la collection."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 body: dict[str, Any] | None = None,
                 params: dict[str, str] | None = None,
                 timeout: float | None = None) -> Any:
        url = f'{self.base_url}{path}'
        if params:
            url = f'{url}?{urllib.parse.urlencode(params)}'

        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                raw = response.read().decode('utf-8')
                return json.loads(raw) if raw else None

        except urllib.error.HTTPError as exc:
            payload = _read_error_payload(exc)
            # Le serveur renvoie un message français exploitable tel quel.
            message = payload.get('error') or exc.reason or 'Erreur inconnue'
            if exc.code == 409:
                raise AlbumConflict(message, status=409, payload=payload) from exc
            raise ApiError(f'{method} {path} → HTTP {exc.code} : {message}',
                           status=exc.code, payload=payload) from exc

        except urllib.error.URLError as exc:
            raise ApiError(
                f'Serveur injoignable sur {self.base_url} : {exc.reason}'
            ) from exc

        except json.JSONDecodeError as exc:
            raise ApiError(
                f'{method} {path} : réponse illisible du serveur'
            ) from exc

    # -- collection --------------------------------------------------------

    def ping(self) -> bool:
        """Teste la disponibilité du serveur avant de graver quoi que ce soit."""
        try:
            self._request('GET', '/api/health', timeout=5.0)
            return True
        except ApiError:
            return False

    def discogs_credentials(self) -> tuple[str, str] | None:
        """Identifiants Discogs enregistrés côté serveur, s'ils existent.

        Le serveur expose ses réglages sur /api/settings, dont le couple
        clé/secret d'application Discogs. Les reprendre évite d'avoir à créer
        un second jeu d'identifiants pour le ripper.

        Renvoie None dès que le compte n'est pas exploitable — route absente,
        champs vides, ou serveur muet. L'appelant se rabat alors sur sa propre
        configuration.
        """
        try:
            settings = self._request('GET', '/api/settings', timeout=5.0)
        except ApiError:
            return None

        if not isinstance(settings, dict):
            return None

        key = str(settings.get('discogs_key') or '').strip()
        secret = str(settings.get('discogs_secret') or '').strip()

        if not key or not secret:
            return None

        # Discogs délivre une clé et un secret distincts (24 et 40 caractères).
        # Deux valeurs identiques trahissent un réglage mal rempli, qui se
        # ferait refuser en 401 : autant l'ignorer tout de suite.
        if key == secret:
            return None

        return key, secret

    def find_duplicate(self, title: str, artist: str) -> dict[str, Any] | None:
        """L'album existe-t-il déjà ? Renvoie la fiche serveur, ou None.

        Attention : cette route compare les chaînes à égalité stricte (au
        COLLATE NOCASE près), alors que le scanner normalise bien plus
        largement (accents, articles, « (Remaster) »). Un None ne garantit
        donc pas l'absence d'un album qui s'apparierait quand même — d'où la
        confirmation demandée à l'utilisateur avant toute création.
        """
        result = self._request('GET', '/api/albums/duplicate',
                               params={'title': title, 'artist': artist})
        if isinstance(result, dict) and result.get('duplicate'):
            return result.get('album') or {}
        return None

    def create_album(self, album: AlbumPayload) -> dict[str, Any]:
        """Crée l'album et ses pistes. Lève AlbumConflict en cas de 409.

        Si cover_url pointe vers Cover Art Archive ou Discogs, le serveur
        télécharge lui-même la pochette et la remplace par une URL locale.
        """
        return self._request('POST', '/api/albums', body=album.to_json())

    def update_album(self, album_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        return self._request('PATCH', f'/api/albums/{album_id}', body=changes)

    def clear_wanted(self, album_id: int) -> dict[str, Any]:
        """Sort l'album de la liste de souhaits — il est désormais possédé."""
        return self.update_album(album_id, {'is_wanted': False})

    # -- appariement des fichiers -----------------------------------------

    def start_scan(self) -> dict[str, Any]:
        """Lance l'appariement fichiers ↔ collection.

        À n'appeler qu'une fois les fichiers en place : le scan repart de
        zéro à chaque fois (clearAllFilePaths puis réappariement complet).
        """
        return self._request('POST', '/api/player/scan', timeout=SCAN_TIMEOUT)

    def scan_status(self) -> dict[str, Any]:
        return self._request('GET', '/api/player/scan/status')


def _read_error_payload(exc: urllib.error.HTTPError) -> dict[str, Any]:
    """Extrait le corps JSON d'une erreur, sans jamais lever à son tour."""
    try:
        raw = exc.read().decode('utf-8')
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
