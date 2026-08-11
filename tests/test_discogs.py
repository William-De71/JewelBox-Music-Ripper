"""Le repli Discogs doit produire un DiscInfo aussi exploitable que MusicBrainz.

Particularité de la source : Discogs décrit des supports physiques de toute
nature, avec des positions libres (« A1 »), des en-têtes de section et des
champs souvent vides. La conversion doit ramener tout ça au modèle du ripper.
"""

import json
import urllib.error
from io import BytesIO

import pytest

from jewelbox_ripper.discogs import (
    MAX_RESULTS,
    DiscogsAuthError,
    DiscogsClient,
    DiscogsError,
    build_disc_info,
)


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code):
    return urllib.error.HTTPError('https://api.discogs.com/x', code,
                                  'Erreur', {}, BytesIO(b''))


@pytest.fixture
def client():
    return DiscogsClient('jeton-de-test')


# -- authentification ----------------------------------------------------


class TestAuthentification:
    def test_token_vide_refuse(self):
        # Sans token, la route de recherche est fermée : autant échouer tout
        # de suite plutôt qu'au premier appel.
        with pytest.raises(DiscogsAuthError):
            DiscogsClient('')

    def test_token_dans_l_entete(self, client, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['headers'] = dict(request.headers)
            return FakeResponse({'results': []})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        client.search('Nirvana', 'Nevermind')

        # urllib capitalise les noms d'en-têtes.
        assert captured['headers']['Authorization'] == 'Discogs token=jeton-de-test'
        assert 'JewelBoxMusicRipper' in captured['headers']['User-agent']

    @pytest.mark.parametrize('code', [401, 403])
    def test_token_refuse_par_le_service(self, client, monkeypatch, code):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: (_ for _ in ()).throw(http_error(code)))

        with pytest.raises(DiscogsAuthError) as exc:
            client.search('A', 'B')
        assert 'discogs.com/settings/developers' in str(exc.value)

    def test_quota_depasse(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: (_ for _ in ()).throw(http_error(429)))

        with pytest.raises(DiscogsError) as exc:
            client.search('A', 'B')
        assert '60 requêtes' in str(exc.value)

    def test_service_injoignable(self, client, monkeypatch):
        def boom(request, timeout=None):
            raise urllib.error.URLError('réseau coupé')

        monkeypatch.setattr('urllib.request.urlopen', boom)
        with pytest.raises(DiscogsError):
            client.search('A', 'B')

    def test_reponse_illisible(self, client, monkeypatch):
        class Garbage:
            def read(self):
                return b'<html>pas du json</html>'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: Garbage())
        with pytest.raises(DiscogsError):
            client.search('A', 'B')

    def test_autre_erreur_http(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: (_ for _ in ()).throw(http_error(500)))
        with pytest.raises(DiscogsError):
            client.search('A', 'B')


# -- recherche -----------------------------------------------------------


class TestRecherche:
    def test_parametres_transmis(self, client, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            return FakeResponse({'results': []})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        client.search('Nirvana', 'Nevermind')

        assert 'artist=Nirvana' in captured['url']
        assert 'release_title=Nevermind' in captured['url']
        assert 'type=release' in captured['url']

    def test_recherche_vide_refusee(self, client):
        # Discogs renverrait le catalogue entier.
        with pytest.raises(DiscogsError):
            client.search('', '')

    def test_un_seul_critere_suffit(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: FakeResponse({'results': []}))
        assert client.search('', 'Nevermind') == []

    def test_titre_scinde_en_artiste_et_album(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [{'id': 1, 'title': 'Nirvana - Nevermind',
                         'year': '1991', 'country': 'US',
                         'label': ['DGC', 'Sub Pop'], 'catno': 'DGCD-24425',
                         'format': ['CD', 'Album']}],
        }))

        [release] = client.search('Nirvana', 'Nevermind')
        assert release.artist == 'Nirvana'
        assert release.title == 'Nevermind'
        assert release.year == 1991
        assert release.label == 'DGC'      # le premier seulement
        assert release.formats == 'CD, Album'

    def test_titre_sans_separateur(self, client, monkeypatch):
        # Certaines entrées n'ont pas la forme « Artiste - Titre ».
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [{'id': 2, 'title': 'CompilationSansTiret'}],
        }))

        [release] = client.search('X', 'Y')
        assert release.title == 'CompilationSansTiret'
        assert release.artist == 'Artiste inconnu'

    def test_liste_tronquee(self, client, monkeypatch):
        many = [{'id': i, 'title': f'A - Album {i}'} for i in range(50)]
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: FakeResponse({'results': many}))

        assert len(client.search('A', 'B')) == MAX_RESULTS

    def test_bon_nombre_de_pistes_en_tete(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [
                {'id': 1, 'title': 'A - Vinyle', 'num_tracks': 10},
                {'id': 2, 'title': 'A - CD', 'num_tracks': 12},
            ],
        }))

        releases = client.search('A', 'B', track_count=12)
        # L'édition à 12 pistes remonte, sans écraser l'ordre des autres.
        assert releases[0].release_id == 2

    def test_tri_stable_sans_correspondance(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [
                {'id': 1, 'title': 'A - Un'},
                {'id': 2, 'title': 'A - Deux'},
            ],
        }))

        releases = client.search('A', 'B', track_count=12)
        assert [r.release_id for r in releases] == [1, 2]

    def test_nombre_de_pistes_illisible(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [{'id': 1, 'title': 'A - B', 'num_tracks': 'douze'}],
        }))

        [release] = client.search('A', 'B')
        assert release.track_count is None

    def test_resume_sans_details(self, client, monkeypatch):
        # Une entrée dépouillée ne doit pas produire de parenthèses vides ni
        # de crochets orphelins dans la liste de choix.
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [{'id': 1, 'title': 'A - B'}],
        }))

        resume = client.search('A', 'B')[0].summary()
        assert resume == 'A — B'
        assert '(' not in resume and '[' not in resume

    def test_resume_avec_catalogue_seul(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [{'id': 1, 'title': 'A - B', 'catno': 'CAT-1'}],
        }))
        assert '(CAT-1)' in client.search('A', 'B')[0].summary()

    def test_resume_lisible(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen', lambda r, timeout=None: FakeResponse({
            'results': [{'id': 1, 'title': 'Nirvana - Nevermind', 'year': '1991',
                         'country': 'US', 'label': ['DGC'], 'catno': 'DGCD-24425',
                         'format': ['CD'], 'num_tracks': 12}],
        }))

        [release] = client.search('N', 'N')
        resume = release.summary()
        assert 'Nirvana' in resume and 'Nevermind' in resume
        assert '1991' in resume and 'DGC' in resume
        assert '12 pistes' in resume


class TestTracklist:
    def test_appel_sur_l_identifiant(self, client, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            return FakeResponse({'id': 367153, 'title': 'Nevermind'})

        monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
        client.fetch_tracklist(367153)
        assert captured['url'].endswith('/releases/367153')

    def test_reponse_inattendue(self, client, monkeypatch):
        monkeypatch.setattr('urllib.request.urlopen',
                            lambda r, timeout=None: FakeResponse(['pas', 'un', 'objet']))
        with pytest.raises(DiscogsError):
            client.fetch_tracklist(1)


# -- conversion ----------------------------------------------------------


NEVERMIND = {
    'id': 367153,
    'title': 'Nevermind',
    'year': 1991,
    'genres': ['Rock'],
    'artists': [{'name': 'Nirvana (2)', 'join': ''}],
    'labels': [{'name': 'DGC'}],
    'identifiers': [{'type': 'Barcode', 'value': '7 20642-42425-2 4'}],
    'images': [
        {'type': 'secondary', 'uri': 'https://img.discogs.com/back.jpg'},
        {'type': 'primary', 'uri': 'https://img.discogs.com/front.jpg'},
    ],
    'tracklist': [
        {'type_': 'heading', 'title': 'Face A', 'position': ''},
        {'type_': 'track', 'position': 'A1',
         'title': 'Smells Like Teen Spirit', 'duration': '5:01'},
        {'type_': 'track', 'position': 'A2', 'title': 'In Bloom',
         'duration': '4:14'},
        {'type_': 'track', 'position': 'B1', 'title': 'Come As You Are',
         'duration': ''},
    ],
}


class TestConversion:
    def test_champs_principaux(self):
        disc = build_disc_info(NEVERMIND)
        assert disc.artist == 'Nirvana'       # suffixe « (2) » retiré
        assert disc.album == 'Nevermind'
        assert disc.year == 1991
        assert disc.genre == 'Rock'
        assert disc.label == 'DGC'
        assert disc.discogs_release_id == 367153

    def test_entetes_de_section_ignores(self):
        # « Face A » n'est pas une piste : la compter décalerait toute la
        # numérotation, et cdparanoia extrairait les mauvais titres.
        disc = build_disc_info(NEVERMIND)
        assert len(disc.tracks) == 3
        assert 'Face A' not in [t.title for t in disc.tracks]

    def test_numerotation_sequentielle(self):
        # Les positions Discogs sont libres (A1, A2, B1) : seul l'ordre
        # physique compte pour l'extraction.
        disc = build_disc_info(NEVERMIND)
        assert [t.number for t in disc.tracks] == [1, 2, 3]

    def test_durees(self):
        disc = build_disc_info(NEVERMIND)
        assert disc.tracks[0].duration_seconds == 301.0
        assert disc.tracks[2].duration_seconds is None   # champ vide
        assert disc.total_duration == '9:15'

    def test_code_barres_compacte(self):
        # Le serveur attend un EAN sans espaces ni tirets.
        disc = build_disc_info(NEVERMIND)
        assert disc.barcode == '7206424242524'

    def test_pochette_primaire_choisie(self):
        disc = build_disc_info(NEVERMIND)
        assert disc.discogs_cover_url == 'https://img.discogs.com/front.jpg'
        # Sans release MusicBrainz, cover_url bascule sur Discogs.
        assert disc.cover_url == 'https://img.discogs.com/front.jpg'

    def test_pochette_a_defaut_la_premiere(self):
        release = dict(NEVERMIND, images=[
            {'type': 'secondary', 'uri': 'https://img.discogs.com/back.jpg'},
        ])
        disc = build_disc_info(release)
        assert disc.discogs_cover_url == 'https://img.discogs.com/back.jpg'

    def test_sans_pochette(self):
        release = dict(NEVERMIND)
        release.pop('images')
        assert build_disc_info(release).cover_url is None

    def test_release_vide(self):
        # Une release sans rien ne doit pas lever : la CLI refuse ensuite un
        # disque sans piste.
        disc = build_disc_info({})
        assert disc.tracks == []
        assert disc.artist == 'Artiste inconnu'
        assert disc.album == 'Album inconnu'

    def test_piste_sans_titre_ecartee(self):
        release = dict(NEVERMIND, tracklist=[
            {'type_': 'track', 'title': '   ', 'duration': '3:00'},
            {'type_': 'track', 'title': 'Vraie piste', 'duration': '3:00'},
        ])
        disc = build_disc_info(release)
        assert [t.title for t in disc.tracks] == ['Vraie piste']

    def test_piste_sans_type_acceptee(self):
        # Le champ type_ est absent sur d'anciennes entrées.
        release = dict(NEVERMIND, tracklist=[
            {'position': '1', 'title': 'Sans type', 'duration': '3:00'},
        ])
        assert len(build_disc_info(release).tracks) == 1

    def test_code_barres_absent(self):
        release = dict(NEVERMIND, identifiers=[
            {'type': 'Matrix / Runout', 'value': 'ABC-123'},
        ])
        assert build_disc_info(release).barcode is None

    def test_annee_hors_bornes(self):
        assert build_disc_info(dict(NEVERMIND, year=0)).year is None


class TestArtistes:
    def test_liaison_feat(self):
        release = dict(NEVERMIND, artists=[
            {'name': 'Queen', 'join': '&'},
            {'name': 'David Bowie', 'join': ''},
        ])
        assert build_disc_info(release).artist == 'Queen & David Bowie'

    def test_liaison_virgule(self):
        release = dict(NEVERMIND, artists=[
            {'name': 'A', 'join': ','},
            {'name': 'B', 'join': ''},
        ])
        assert build_disc_info(release).artist == 'A,B'

    def test_artiste_par_piste(self):
        release = dict(NEVERMIND, tracklist=[
            {'type_': 'track', 'title': 'Un', 'duration': '3:00',
             'artists': [{'name': 'Alpha', 'join': ''}]},
        ])
        assert build_disc_info(release).tracks[0].artist == 'Alpha'

    def test_artistes_non_liste(self):
        assert build_disc_info(dict(NEVERMIND, artists='Nirvana')).artist \
            == 'Artiste inconnu'


class TestDonneesMalformees:
    """Discogs est alimenté par ses utilisateurs : tout champ peut manquer,
    être vide ou avoir un type inattendu. Rien de tout ça ne doit lever."""

    @pytest.mark.parametrize('duree, attendu', [
        ('4:38', 278.0),
        ('1:02:11', 3731.0),
        ('', None),
        (None, None),
        ('3', None),            # une seule valeur : ambiguë, donc ignorée
        ('1:2:3:4', None),      # quatre segments : hors norme
        ('abc', None),
        ('4:xx', None),
    ])
    def test_durees_tordues(self, duree, attendu):
        release = dict(NEVERMIND, tracklist=[
            {'type_': 'track', 'title': 'Piste', 'duration': duree},
        ])
        assert build_disc_info(release).tracks[0].duration_seconds == attendu

    @pytest.mark.parametrize('valeur, attendu', [
        (1991, 1991),
        ('1991', 1991),
        ('1991-09-24', 1991),
        ('', None),
        (None, None),
        ('inconnu', None),
        (3000, None),           # hors bornes
    ])
    def test_annees_tordues(self, valeur, attendu):
        assert build_disc_info(dict(NEVERMIND, year=valeur)).year == attendu

    def test_identifiant_non_dictionnaire(self):
        release = dict(NEVERMIND, identifiers=['chaîne inattendue'])
        assert build_disc_info(release).barcode is None

    def test_code_barres_vide(self):
        release = dict(NEVERMIND, identifiers=[
            {'type': 'Barcode', 'value': '   '},
        ])
        assert build_disc_info(release).barcode is None

    def test_image_non_dictionnaire(self):
        release = dict(NEVERMIND, images=['chaîne inattendue'])
        assert build_disc_info(release).discogs_cover_url is None

    def test_image_primaire_sans_url(self):
        # Repli sur resource_url quand uri manque.
        release = dict(NEVERMIND, images=[
            {'type': 'primary', 'resource_url': 'https://img.discogs.com/r.jpg'},
        ])
        assert build_disc_info(release).discogs_cover_url \
            == 'https://img.discogs.com/r.jpg'

    def test_images_sans_aucune_url(self):
        release = dict(NEVERMIND, images=[{'type': 'primary'}, {'type': 'x'}])
        assert build_disc_info(release).discogs_cover_url is None

    def test_artiste_non_dictionnaire_ignore(self):
        release = dict(NEVERMIND, artists=[
            'chaîne inattendue',
            {'name': 'Valide', 'join': ''},
        ])
        assert build_disc_info(release).artist == 'Valide'

    def test_artiste_sans_nom_ignore(self):
        release = dict(NEVERMIND, artists=[
            {'name': '', 'join': ''},
            {'name': 'Valide', 'join': ''},
        ])
        assert build_disc_info(release).artist == 'Valide'

    def test_label_sans_nom(self):
        assert build_disc_info(dict(NEVERMIND, labels=[{}])).label is None

    def test_genres_vides(self):
        assert build_disc_info(dict(NEVERMIND, genres=[])).genre is None

    def test_release_sans_identifiant(self):
        release = dict(NEVERMIND)
        release.pop('id')
        assert build_disc_info(release).discogs_release_id is None


class TestCompilations:
    def test_various_artists(self):
        release = dict(NEVERMIND, artists=[{'name': 'Various', 'join': ''}])
        disc = build_disc_info(release)
        assert disc.is_compilation
        assert disc.album_artist() == 'Artistes divers'

    def test_pistes_heterogenes(self):
        release = dict(NEVERMIND, artists=[{'name': 'Collectif', 'join': ''}],
                       tracklist=[
            {'type_': 'track', 'title': 'Un', 'artists': [{'name': 'A'}]},
            {'type_': 'track', 'title': 'Deux', 'artists': [{'name': 'B'}]},
            {'type_': 'track', 'title': 'Trois', 'artists': [{'name': 'C'}]},
        ])
        assert build_disc_info(release).is_compilation

    def test_album_normal_pas_compilation(self):
        assert not build_disc_info(NEVERMIND).is_compilation
