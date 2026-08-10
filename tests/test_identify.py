"""Conversion des réponses MusicBrainz en DiscInfo.

Les données de test reproduisent la structure réelle renvoyée par
musicbrainzngs.get_releases_by_discid().
"""

import sys
import types

import pytest

from jewelbox_ripper.identify import (
    DiscReadError,
    IdentificationError,
    ReleaseCandidate,
    _join_artist_credit,
    _looks_like_compilation,
    _parse_year,
    _select_medium,
    build_disc_info,
    lookup_candidates,
    read_disc_id,
)
from jewelbox_ripper.disc import TrackInfo


def _release(**overrides):
    base = {
        'id': 'release-abc',
        'title': 'Animals',
        'artist-credit': [{'artist': {'name': 'Pink Floyd'}}],
        'date': '1977-01-23',
        'country': 'GB',
        'barcode': '509990298762',
        'label-info-list': [{'label': {'name': 'Harvest'}}],
        'medium-list': [{
            'position': '1',
            'track-count': 5,
            'disc-list': [{'id': 'disc-xyz'}],
            'track-list': [
                {'position': '1', 'length': '85000',
                 'recording': {'title': 'Pigs on the Wing 1'}},
                {'position': '2', 'length': '1027000',
                 'recording': {'title': 'Dogs'}},
            ],
        }],
    }
    base.update(overrides)
    return base


def _candidate(**overrides):
    release = _release(**overrides)
    return ReleaseCandidate(
        release_id=release['id'],
        title=release['title'],
        artist='Pink Floyd',
        date=release.get('date'),
        country=release.get('country'),
        label='Harvest',
        barcode=release.get('barcode'),
        track_count=5,
        disambiguation=None,
        raw=release,
    )


class TestBuildDiscInfo:
    def test_champs_principaux(self):
        disc = build_disc_info(_candidate(), disc_id='disc-xyz')

        assert disc.artist == 'Pink Floyd'
        assert disc.album == 'Animals'
        assert disc.year == 1977
        assert disc.label == 'Harvest'
        assert disc.barcode == '509990298762'
        assert disc.musicbrainz_release_id == 'release-abc'

    def test_pistes_converties(self):
        disc = build_disc_info(_candidate(), disc_id='disc-xyz')

        assert len(disc.tracks) == 2
        assert disc.tracks[0].title == 'Pigs on the Wing 1'
        # La longueur MusicBrainz est en millisecondes.
        assert disc.tracks[0].duration_seconds == 85.0
        assert disc.tracks[1].duration_label == '17:07'

    def test_pistes_triees(self):
        media = _release()['medium-list']
        media[0]['track-list'].reverse()
        disc = build_disc_info(_candidate(**{'medium-list': media}),
                               disc_id='disc-xyz')
        assert [t.number for t in disc.tracks] == [1, 2]

    def test_support_choisi_par_disc_id(self):
        # Sur un coffret, seul le disque physiquement inséré doit être extrait.
        media = [
            {'position': '1', 'disc-list': [{'id': 'disc-un'}],
             'track-list': [{'position': '1',
                             'recording': {'title': 'Du disque 1'}}]},
            {'position': '2', 'disc-list': [{'id': 'disc-deux'}],
             'track-list': [{'position': '1',
                             'recording': {'title': 'Du disque 2'}}]},
        ]
        disc = build_disc_info(_candidate(**{'medium-list': media}),
                               disc_id='disc-deux')

        assert disc.disc_number == 2
        assert disc.disc_total == 2
        assert disc.tracks[0].title == 'Du disque 2'

    def test_sans_duree(self):
        media = _release()['medium-list']
        media[0]['track-list'] = [
            {'position': '1', 'recording': {'title': 'Sans durée'}}]
        disc = build_disc_info(_candidate(**{'medium-list': media}),
                               disc_id='disc-xyz')
        assert disc.tracks[0].duration_seconds is None


class TestArtistCredit:
    def test_artiste_simple(self):
        assert _join_artist_credit(
            [{'artist': {'name': 'Pink Floyd'}}]) == 'Pink Floyd'

    def test_collaboration(self):
        credit = [
            {'artist': {'name': 'Bowie'}, 'joinphrase': ' & '},
            {'artist': {'name': 'Queen'}},
        ]
        assert _join_artist_credit(credit) == 'Bowie & Queen'

    def test_credit_vide(self):
        assert _join_artist_credit([]) == ''


class TestCompilation:
    def test_various_artists(self):
        assert _looks_like_compilation('Various Artists', [])
        assert _looks_like_compilation('various', [])

    def test_artiste_unique(self):
        tracks = [TrackInfo(1, 'A', artist='Pink Floyd'),
                  TrackInfo(2, 'B', artist='Pink Floyd')]
        assert not _looks_like_compilation('Pink Floyd', tracks)

    def test_artistes_heterogenes(self):
        tracks = [TrackInfo(i, f'T{i}', artist=f'Artiste {i}')
                  for i in range(1, 5)]
        assert _looks_like_compilation('Divers', tracks)


class TestParseYear:
    def test_date_complete(self):
        assert _parse_year('1977-01-23') == 1977

    def test_annee_seule(self):
        assert _parse_year('1977') == 1977

    def test_absente_ou_invalide(self):
        assert _parse_year(None) is None
        assert _parse_year('inconnue') is None


class TestSummary:
    def test_ligne_lisible(self):
        summary = _candidate().summary()
        assert 'Pink Floyd' in summary
        assert 'Animals' in summary
        assert '5 pistes' in summary

    def test_ligne_avec_desambiguisation(self):
        candidate = _candidate()
        candidate.disambiguation = 'coffret collector'
        assert '[coffret collector]' in candidate.summary()

    def test_ligne_sans_details(self):
        candidate = _candidate()
        candidate.date = candidate.country = candidate.label = None
        summary = candidate.summary()
        # Pas de parenthèses vides quand aucun détail n'est connu.
        assert '()' not in summary


class FakeDisc:
    def __init__(self, disc_id='ABC-123', tracks=12):
        self.id = disc_id
        self.tracks = list(range(tracks))


def _fake_discid(monkeypatch, *, disc=None, error=None):
    """Installe un faux module « discid » importable par read_disc_id()."""
    module = types.ModuleType('discid')

    def read(device=None):
        if error:
            raise error
        module.seen_device = device
        return disc or FakeDisc()

    module.read = read
    module.seen_device = None
    monkeypatch.setitem(sys.modules, 'discid', module)
    return module


class TestReadDiscId:
    def test_lecture_reussie(self, monkeypatch):
        _fake_discid(monkeypatch, disc=FakeDisc('XYZ-789', 13))
        disc_id, count = read_disc_id()

        assert disc_id == 'XYZ-789'
        assert count == 13

    def test_device_transmis(self, monkeypatch):
        module = _fake_discid(monkeypatch)
        read_disc_id('/dev/sr1')
        assert module.seen_device == '/dev/sr1'

    def test_sans_device_detection_auto(self, monkeypatch):
        module = _fake_discid(monkeypatch)
        read_disc_id()
        # Aucun device passé : discid choisit le lecteur par défaut.
        assert module.seen_device is None

    def test_disque_illisible(self, monkeypatch):
        _fake_discid(monkeypatch,
                     error=RuntimeError("cannot open device `/dev/cdrom'"))

        with pytest.raises(DiscReadError) as excinfo:
            read_disc_id()

        message = str(excinfo.value)
        assert 'Lecture du disque impossible' in message
        # Le message doit orienter l'utilisateur.
        assert 'CD audio est inséré' in message

    def test_module_absent(self, monkeypatch):
        # Simule une installation sans python3-discid.
        monkeypatch.setitem(sys.modules, 'discid', None)

        with pytest.raises(DiscReadError, match='discid'):
            read_disc_id()


class FakeMusicBrainzError(Exception):
    """Tient le rôle de musicbrainzngs.MusicBrainzError (réseau, service HS)."""


class FakeResponseError(FakeMusicBrainzError):
    """Tient le rôle de musicbrainzngs.ResponseError (404 : disque inconnu).

    L'héritage reproduit celui de la vraie bibliothèque : le code intercepte
    ResponseError avant MusicBrainzError, et cet ordre n'a de sens que si le
    premier dérive du second.
    """


def _fake_musicbrainzngs(monkeypatch, *, result=None, error=None):
    """Installe un faux module « musicbrainzngs »."""
    module = types.ModuleType('musicbrainzngs')

    module.MusicBrainzError = FakeMusicBrainzError
    module.ResponseError = FakeResponseError
    module.set_useragent = lambda *a: module.__dict__.__setitem__('agent', a)

    def get_releases_by_discid(disc_id, includes=None):
        module.seen = {'disc_id': disc_id, 'includes': includes}
        if error:
            raise error
        return result or {}

    module.get_releases_by_discid = get_releases_by_discid
    monkeypatch.setitem(sys.modules, 'musicbrainzngs', module)
    return module


class TestLookupCandidates:
    def test_reponse_sous_disc(self, monkeypatch):
        _fake_musicbrainzngs(monkeypatch, result={
            'disc': {'release-list': [_release()]},
        })
        candidates = lookup_candidates('ABC-123')

        assert len(candidates) == 1
        assert candidates[0].title == 'Animals'
        assert candidates[0].artist == 'Pink Floyd'

    def test_reponse_sous_release_list(self, monkeypatch):
        # Cas du TOC reconnu sans Disc ID enregistré.
        _fake_musicbrainzngs(monkeypatch, result={'release-list': [_release()]})
        assert len(lookup_candidates('ABC-123')) == 1

    def test_plusieurs_editions(self, monkeypatch):
        _fake_musicbrainzngs(monkeypatch, result={
            'disc': {'release-list': [_release(), _release(id='autre')]},
        })
        assert len(lookup_candidates('ABC-123')) == 2

    def test_user_agent_declare(self, monkeypatch):
        # MusicBrainz exige un User-Agent identifiant l'application.
        module = _fake_musicbrainzngs(monkeypatch, result={})
        lookup_candidates('ABC-123')
        assert module.agent[0] == 'jewelbox-music-ripper'

    def test_disque_inconnu_liste_vide(self, monkeypatch):
        # 404 : pas une erreur, on bascule vers la saisie manuelle.
        _fake_musicbrainzngs(monkeypatch, error=FakeResponseError('404'))
        assert lookup_candidates('ABC-123') == []

    def test_reponse_vide(self, monkeypatch):
        _fake_musicbrainzngs(monkeypatch, result={})
        assert lookup_candidates('ABC-123') == []

    def test_service_injoignable(self, monkeypatch):
        _fake_musicbrainzngs(monkeypatch,
                             error=FakeMusicBrainzError('connexion refusée'))

        with pytest.raises(IdentificationError, match='injoignable'):
            lookup_candidates('ABC-123')

    def test_includes_demandes(self, monkeypatch):
        module = _fake_musicbrainzngs(monkeypatch, result={})
        lookup_candidates('ABC-123')

        # Sans ces includes, ni les titres de pistes ni le label n'arrivent.
        includes = module.seen['includes']
        assert 'recordings' in includes
        assert 'artists' in includes
        assert 'labels' in includes

    def test_module_absent(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'musicbrainzngs', None)
        with pytest.raises(IdentificationError, match='musicbrainzngs'):
            lookup_candidates('ABC-123')


class TestToCandidate:
    def test_sans_label(self, monkeypatch):
        _fake_musicbrainzngs(monkeypatch, result={
            'disc': {'release-list': [_release(**{'label-info-list': []})]},
        })
        assert lookup_candidates('ABC')[0].label is None

    def test_code_barres_vide_devient_none(self, monkeypatch):
        _fake_musicbrainzngs(monkeypatch, result={
            'disc': {'release-list': [_release(barcode='')]},
        })
        assert lookup_candidates('ABC')[0].barcode is None

    def test_champs_manquants_tolérés(self, monkeypatch):
        # Une release très incomplète ne doit pas faire planter l'outil.
        _fake_musicbrainzngs(monkeypatch, result={
            'disc': {'release-list': [{'id': 'x'}]},
        })
        candidate = lookup_candidates('ABC')[0]

        assert candidate.title == 'Album inconnu'
        assert candidate.artist == 'Artiste inconnu'
        assert candidate.track_count == 0


class TestSelectMedium:
    def test_sans_media(self):
        medium, number, total = _select_medium({}, None)
        assert medium is None
        assert (number, total) == (1, 1)

    def test_disc_id_inconnu_replie_sur_le_premier(self):
        media = [{'position': '1', 'disc-list': [{'id': 'autre'}]}]
        medium, number, total = _select_medium({'medium-list': media},
                                               'introuvable')
        assert medium is media[0]
        assert number == 1

    def test_sans_disc_id_prend_le_premier(self):
        media = [{'position': '3', 'disc-list': []}]
        _, number, total = _select_medium({'medium-list': media}, None)
        assert number == 3
        assert total == 1
