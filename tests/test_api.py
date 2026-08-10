"""Le client API doit respecter le contrat des routes Fastify du serveur."""

import json
import urllib.error
from io import BytesIO

import pytest

from jewelbox_ripper.api import (
    AlbumConflict,
    AlbumPayload,
    ApiError,
    JewelBoxApi,
    TrackPayload,
)


class TestPayloads:
    def test_champs_requis_seulement(self):
        # Le schéma Fastify n'exige que title et artist_name ; envoyer des
        # champs vides écraserait des valeurs côté serveur.
        payload = AlbumPayload(title='Album', artist_name='Artiste').to_json()
        assert payload == {'title': 'Album', 'artist_name': 'Artiste'}

    def test_omet_les_valeurs_vides(self):
        payload = AlbumPayload(title='A', artist_name='B', genre='',
                               year=None, ean='').to_json()
        assert 'genre' not in payload
        assert 'year' not in payload
        assert 'ean' not in payload

    def test_inclut_les_champs_renseignes(self):
        payload = AlbumPayload(
            title='A', artist_name='B', year=1979, genre='Rock',
            label_name='Harvest', ean='509990298762',
            cover_url='https://coverartarchive.org/release/abc/front',
        ).to_json()
        assert payload['year'] == 1979
        assert payload['genre'] == 'Rock'
        assert payload['label_name'] == 'Harvest'
        assert payload['ean'] == '509990298762'

    def test_pistes_serialisees(self):
        payload = AlbumPayload(
            title='A', artist_name='B',
            tracks=[TrackPayload(1, 'Un', '3:45'), TrackPayload(2, 'Deux')],
        ).to_json()
        assert payload['tracks'] == [
            {'position': 1, 'title': 'Un', 'duration': '3:45'},
            {'position': 2, 'title': 'Deux'},
        ]


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def api():
    return JewelBoxApi('http://localhost:3001')


class TestFindDuplicate:
    def test_album_present(self, api, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            return FakeResponse({'duplicate': True,
                                 'album': {'id': 12, 'title': 'The Wall',
                                           'is_wanted': 0}})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        found = api.find_duplicate('The Wall', 'Pink Floyd')

        assert found['id'] == 12
        assert 'title=The+Wall' in captured['url']
        assert 'artist=Pink+Floyd' in captured['url']

    def test_album_absent(self, api, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: FakeResponse({'duplicate': False}))
        assert api.find_duplicate('Inconnu', 'Personne') is None

    def test_accents_encodes(self, api, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            return FakeResponse({'duplicate': False})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        api.find_duplicate('Éponyme', 'Téléphone')
        # Pas d'octet brut non-ASCII dans l'URL.
        captured['url'].encode('ascii')


class TestCreateAlbum:
    def test_envoie_du_json(self, api, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['body'] = json.loads(request.data)
            captured['method'] = request.get_method()
            return FakeResponse({'id': 7, 'title': 'Album'})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        created = api.create_album(AlbumPayload(title='Album',
                                                artist_name='Artiste'))

        assert created['id'] == 7
        assert captured['method'] == 'POST'
        assert captured['body']['title'] == 'Album'

    def test_conflit_409(self, api, monkeypatch):
        message = 'Un album avec ce code EAN existe déjà dans votre collection.'

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 409, 'Conflict', {},
                BytesIO(json.dumps({'error': message}).encode('utf-8')))

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

        with pytest.raises(AlbumConflict) as excinfo:
            api.create_album(AlbumPayload(title='A', artist_name='B'))

        # Le message français du serveur est réutilisable tel quel.
        assert str(excinfo.value) == message
        assert excinfo.value.status == 409

    def test_erreur_500(self, api, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 500, 'Server Error', {},
                BytesIO(b'{"error":"boom"}'))

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        with pytest.raises(ApiError) as excinfo:
            api.create_album(AlbumPayload(title='A', artist_name='B'))
        assert excinfo.value.status == 500

    def test_serveur_injoignable(self, api, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError('Connection refused')

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        with pytest.raises(ApiError, match='injoignable'):
            api.create_album(AlbumPayload(title='A', artist_name='B'))


class TestDivers:
    def test_clear_wanted(self, api, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['body'] = json.loads(request.data)
            captured['method'] = request.get_method()
            return FakeResponse({'id': 3, 'is_wanted': 0})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        api.clear_wanted(3)

        assert captured['method'] == 'PATCH'
        assert captured['body'] == {'is_wanted': False}

    def test_ping_faux_si_injoignable(self, api, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError('refused')

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        assert api.ping() is False

    def test_base_url_sans_slash_final(self):
        assert JewelBoxApi('http://x:3001/').base_url == 'http://x:3001'

    def test_ping_vrai_si_disponible(self, api, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: FakeResponse({'status': 'ok'}))
        assert api.ping() is True

    def test_reponse_illisible(self, api, monkeypatch):
        class NotJson:
            def read(self):
                return b'<html>erreur passerelle</html>'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: NotJson())

        with pytest.raises(ApiError, match='illisible'):
            api.find_duplicate('A', 'B')

    def test_reponse_vide_acceptee(self, api, monkeypatch):
        class Empty:
            def read(self):
                return b''

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: Empty())
        # Un corps vide est licite (204) et ne doit pas lever.
        assert api.find_duplicate('A', 'B') is None


class TestScan:
    def test_start_scan(self, api, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['method'] = request.get_method()
            captured['url'] = request.full_url
            captured['timeout'] = timeout
            return FakeResponse({'running': True})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        api.start_scan()

        assert captured['method'] == 'POST'
        assert captured['url'].endswith('/api/player/scan')
        # Le scan parcourt toute la bibliothèque : bien plus long qu'un appel
        # ordinaire, d'où un délai dédié.
        assert captured['timeout'] > 60

    def test_scan_status(self, api, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None:
                            FakeResponse({'running': False,
                                          'result': {'matched_tracks': 13}}))
        status = api.scan_status()

        assert status['running'] is False
        assert status['result']['matched_tracks'] == 13


class TestErrorPayload:
    def test_corps_non_json_ignore(self, api, monkeypatch):
        # Une erreur avec un corps HTML ne doit pas masquer le code HTTP.
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 503, 'Service Unavailable', {},
                BytesIO(b'<html>maintenance</html>'))

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        with pytest.raises(ApiError) as excinfo:
            api.scan_status()

        assert excinfo.value.status == 503
        assert excinfo.value.payload == {}

    def test_conflit_sans_message(self, api, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 409, 'Conflict', {}, BytesIO(b''))

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        with pytest.raises(AlbumConflict) as excinfo:
            api.create_album(AlbumPayload(title='A', artist_name='B'))

        # Faute de message serveur, on retombe sur la raison HTTP.
        assert 'Conflict' in str(excinfo.value)
