"""Modèle de disque : durées, pochette, détection de compilation."""

from jewelbox_ripper.disc import DiscInfo, TrackInfo, format_duration


class TestFormatDuration:
    def test_minutes_secondes(self):
        assert format_duration(225) == '3:45'
        assert format_duration(59) == '0:59'

    def test_au_dela_d_une_heure(self):
        assert format_duration(3725) == '1:02:05'

    def test_arrondi(self):
        assert format_duration(225.6) == '3:46'

    def test_valeurs_invalides(self):
        assert format_duration(None) is None
        assert format_duration(-1) is None


class TestDiscInfo:
    def test_duree_totale(self):
        disc = DiscInfo(artist='A', album='B', tracks=[
            TrackInfo(1, 'Un', 120.0),
            TrackInfo(2, 'Deux', 105.0),
        ])
        assert disc.total_duration == '3:45'

    def test_duree_totale_partielle(self):
        # Une durée inconnue ne doit pas annuler le total des autres.
        disc = DiscInfo(artist='A', album='B', tracks=[
            TrackInfo(1, 'Un', 120.0),
            TrackInfo(2, 'Deux', None),
        ])
        assert disc.total_duration == '2:00'

    def test_duree_totale_absente(self):
        disc = DiscInfo(artist='A', album='B',
                        tracks=[TrackInfo(1, 'Un', None)])
        assert disc.total_duration is None

    def test_url_pochette(self):
        disc = DiscInfo(artist='A', album='B',
                        musicbrainz_release_id='abc-123')
        # Le serveur reconnaît coverartarchive.org et télécharge lui-même.
        assert disc.cover_url == (
            'https://coverartarchive.org/release/abc-123/front')
        assert 'coverartarchive.org' in disc.cover_url

    def test_pas_de_pochette_sans_mbid(self):
        assert DiscInfo(artist='A', album='B').cover_url is None

    def test_artiste_album_normal(self):
        disc = DiscInfo(artist='Pink Floyd', album='Animals')
        assert disc.album_artist() == 'Pink Floyd'

    def test_artiste_album_compilation(self):
        disc = DiscInfo(artist='Various Artists', album='Hits',
                        is_compilation=True)
        assert disc.album_artist() == 'Artistes divers'
