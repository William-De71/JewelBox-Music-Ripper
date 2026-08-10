"""Les tags écrits doivent correspondre à ce que readFileTags() lit.

Le serveur lit albumartist (à défaut artist), album, le n° de piste et le
titre : ce sont eux qui portent l'appariement principal.
"""

from jewelbox_ripper.disc import DiscInfo, TrackInfo
from jewelbox_ripper.tagging import _build_tags


def _disc(**overrides):
    base = dict(
        artist='Pink Floyd',
        album='Animals',
        tracks=[TrackInfo(1, 'Pigs on the Wing 1'), TrackInfo(2, 'Dogs')],
        year=1977,
        genre='Rock',
        label='Harvest',
    )
    base.update(overrides)
    return DiscInfo(**base)


class TestBuildTags:
    def test_champs_lus_par_le_serveur(self):
        disc = _disc()
        tags = _build_tags(disc, disc.tracks[0])

        # Exactement ce que readFileTags() extrait.
        assert tags['album'] == ['Animals']
        assert tags['albumartist'] == ['Pink Floyd']
        assert tags['title'] == ['Pigs on the Wing 1']
        assert tags['tracknumber'] == ['1/2']

    def test_numero_de_piste_avec_total(self):
        disc = _disc()
        assert _build_tags(disc, disc.tracks[1])['tracknumber'] == ['2/2']

    def test_metadonnees_optionnelles(self):
        disc = _disc()
        tags = _build_tags(disc, disc.tracks[0])
        assert tags['date'] == ['1977']
        assert tags['genre'] == ['Rock']
        assert tags['organization'] == ['Harvest']

    def test_champs_absents_omis(self):
        disc = _disc(year=None, genre=None, label=None)
        tags = _build_tags(disc, disc.tracks[0])
        assert 'date' not in tags
        assert 'genre' not in tags
        assert 'organization' not in tags

    def test_numero_de_disque_seulement_si_coffret(self):
        simple = _disc()
        assert 'discnumber' not in _build_tags(simple, simple.tracks[0])

        coffret = _disc(disc_number=2, disc_total=3)
        assert _build_tags(coffret, coffret.tracks[0])['discnumber'] == ['2/3']

    def test_compilation_artiste_de_piste_distinct(self):
        disc = _disc(
            artist='Various Artists',
            is_compilation=True,
            tracks=[TrackInfo(1, 'Un', artist='Bowie')],
        )
        tags = _build_tags(disc, disc.tracks[0])

        # L'album se regroupe sous « Artistes divers », la piste garde le sien.
        assert tags['albumartist'] == ['Artistes divers']
        assert tags['artist'] == ['Bowie']

    def test_artiste_de_piste_par_defaut(self):
        disc = _disc()
        assert _build_tags(disc, disc.tracks[0])['artist'] == ['Pink Floyd']

    def test_mbid_conserve(self):
        disc = _disc(musicbrainz_release_id='abc-123')
        tags = _build_tags(disc, disc.tracks[0])
        assert tags['musicbrainz_albumid'] == ['abc-123']
