"""Les tags écrits doivent correspondre à ce que readFileTags() lit.

Le serveur lit albumartist (à défaut artist), album, le n° de piste et le
titre : ce sont eux qui portent l'appariement principal.
"""

import mutagen
import mutagen.flac
import pytest

from jewelbox_ripper.disc import DiscInfo, TrackInfo
from jewelbox_ripper.tagging import TaggingError, _build_tags, tag_file


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


def _make_flac(path):
    """Écrit un FLAC minimal mais valide, encodé par le vrai binaire flac.

    Taguer un fichier suppose un conteneur réel : mutagen refuse un fichier
    factice, et c'est précisément ce comportement qu'on veut exercer.
    """
    import struct
    import subprocess
    import wave

    wav = path.with_suffix('.wav')
    with wave.open(str(wav), 'wb') as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b''.join(struct.pack('<hh', 0, 0)
                                    for _ in range(1000)))

    result = subprocess.run(['flac', '-8', '--silent', '--force',
                             '-o', str(path), str(wav)],
                            capture_output=True, check=False)
    wav.unlink(missing_ok=True)
    return result.returncode == 0 and path.exists()


class TestTagFile:
    """Écriture réelle sur disque, relue ensuite comme le fait le serveur."""

    def test_tags_ecrits_et_relus(self, tmp_path):
        target = tmp_path / '01 - Pigs on the Wing 1.flac'
        if not _make_flac(target):
            pytest.skip('binaire flac indisponible')

        disc = _disc()
        tag_file(target, disc, disc.tracks[0])

        # Relecture par les mêmes clés « easy » que readFileTags() côté serveur.
        audio = mutagen.File(target, easy=True)
        assert audio['albumartist'] == ['Pink Floyd']
        assert audio['album'] == ['Animals']
        assert audio['title'] == ['Pigs on the Wing 1']
        assert audio['tracknumber'] == ['1/2']
        assert audio['date'] == ['1977']

    def test_accents_preserves(self, tmp_path):
        target = tmp_path / '01 - Ete.flac'
        if not _make_flac(target):
            pytest.skip('binaire flac indisponible')

        disc = _disc(artist='Téléphone', album='Été Éponyme',
                     tracks=[TrackInfo(1, 'Cendrillon')])
        tag_file(target, disc, disc.tracks[0])

        audio = mutagen.File(target, easy=True)
        assert audio['albumartist'] == ['Téléphone']
        assert audio['album'] == ['Été Éponyme']

    def test_format_non_reconnu(self, tmp_path):
        bogus = tmp_path / 'pas-un-audio.flac'
        bogus.write_bytes(b'ceci nest pas du flac')

        disc = _disc()
        with pytest.raises(TaggingError):
            tag_file(bogus, disc, disc.tracks[0])

    def test_fichier_absent(self, tmp_path):
        disc = _disc()
        with pytest.raises(TaggingError, match='impossible'):
            tag_file(tmp_path / 'nexistepas.flac', disc, disc.tracks[0])

    def test_echec_sauvegarde(self, tmp_path, monkeypatch):
        target = tmp_path / '01 - Titre.flac'
        if not _make_flac(target):
            pytest.skip('binaire flac indisponible')

        disc = _disc()

        # Simule un disque plein ou un montage repassé en lecture seule
        # entre l'ouverture et l'écriture.
        def boom(self, *args, **kwargs):
            raise OSError('espace insuffisant')

        monkeypatch.setattr(mutagen.flac.FLAC, 'save', boom)

        with pytest.raises(TaggingError, match='espace insuffisant'):
            tag_file(target, disc, disc.tracks[0])
