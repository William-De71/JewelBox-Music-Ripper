"""Conversion des réponses MusicBrainz en DiscInfo.

Les données de test reproduisent la structure réelle renvoyée par
musicbrainzngs.get_releases_by_discid().
"""

from jewelbox_ripper.identify import (
    ReleaseCandidate,
    _join_artist_credit,
    _looks_like_compilation,
    _parse_year,
    _select_medium,
    build_disc_info,
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
