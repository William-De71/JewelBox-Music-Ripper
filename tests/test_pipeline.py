"""Pipeline : planification des chemins et publication atomique.

Le rip réel est simulé — pas de lecteur optique requis. L'enjeu vérifié ici
est qu'un échec ne laisse jamais d'album partiel dans la bibliothèque.
"""

import pytest

from jewelbox_ripper.config import Config
from jewelbox_ripper.disc import DiscInfo, TrackInfo
from jewelbox_ripper.pipeline import PipelineError, plan_paths, rip_disc
from jewelbox_ripper.ripper import ENCODERS


@pytest.fixture
def disc():
    return DiscInfo(
        artist='Pink Floyd',
        album='Animals',
        tracks=[
            TrackInfo(1, 'Pigs on the Wing 1', 85.0),
            TrackInfo(2, 'Dogs', 1027.0),
        ],
        year=1977,
    )


@pytest.fixture
def config(tmp_path):
    return Config(library_path=tmp_path, server_url='http://localhost:3001')


class TestPlanPaths:
    def test_chemins_attendus(self, disc, config):
        album_dir, planned = plan_paths(disc, config)

        assert album_dir == config.library_path / 'Pink Floyd' / 'Animals'
        assert [p.name for _, p in planned] == [
            '01 - Pigs on the Wing 1.flac',
            '02 - Dogs.flac',
        ]

    def test_extension_suit_le_format(self, disc, config):
        config.audio_format = 'opus'
        _, planned = plan_paths(disc, config)
        assert all(p.suffix == '.opus' for _, p in planned)

    def test_coffret_ajoute_un_sous_dossier(self, disc, config):
        disc.disc_number, disc.disc_total = 2, 3
        album_dir, _ = plan_paths(disc, config)
        assert album_dir.name == 'CD2'

    def test_compilation_classee_sous_artistes_divers(self, disc, config):
        disc.is_compilation = True
        album_dir, _ = plan_paths(disc, config)
        assert 'Artistes divers' in str(album_dir)

    def test_aucune_ecriture(self, disc, config):
        album_dir, _ = plan_paths(disc, config)
        assert not album_dir.exists()


def _fake_rip(monkeypatch, *, failing_tracks=()):
    """Simule rip_and_encode : écrit un fichier factice, ou échoue."""
    def fake(track, output, spec, **kwargs):
        if track.number in failing_tracks:
            raise RuntimeError(f'disque rayé sur la piste {track.number}')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'audio-simule')
        return output

    monkeypatch.setattr('jewelbox_ripper.pipeline.rip_and_encode', fake)
    monkeypatch.setattr('jewelbox_ripper.pipeline.tag_file',
                        lambda path, disc, track: None)


class TestRipDisc:
    def test_rip_complet(self, disc, config, monkeypatch):
        _fake_rip(monkeypatch)
        result = rip_disc(disc, config, ENCODERS['flac'])

        assert result.ok
        assert len(result.files) == 2
        assert all(f.exists() for f in result.files)
        assert result.files[0].name == '01 - Pigs on the Wing 1.flac'

    def test_refuse_un_dossier_deja_rempli(self, disc, config, monkeypatch):
        _fake_rip(monkeypatch)
        album_dir = config.library_path / 'Pink Floyd' / 'Animals'
        album_dir.mkdir(parents=True)
        (album_dir / 'deja-la.flac').write_bytes(b'x')

        with pytest.raises(PipelineError, match='existe déjà'):
            rip_disc(disc, config, ENCODERS['flac'])

    def test_disque_sans_piste(self, config):
        with pytest.raises(PipelineError, match='Aucune piste'):
            rip_disc(DiscInfo(artist='A', album='B'), config, ENCODERS['flac'])

    def test_piste_en_echec_signalee(self, disc, config, monkeypatch):
        _fake_rip(monkeypatch, failing_tracks={2})
        result = rip_disc(disc, config, ENCODERS['flac'])

        # Les pistes lisibles sont conservées, l'échec est remonté.
        assert not result.ok
        assert len(result.files) == 1
        assert result.failed[0][0] == 2
        assert 'rayé' in result.failed[0][1]

    def test_echec_total_ne_laisse_rien(self, disc, config, monkeypatch):
        _fake_rip(monkeypatch, failing_tracks={1, 2})

        with pytest.raises(PipelineError, match='Aucune piste'):
            rip_disc(disc, config, ENCODERS['flac'])

        # Le point clé : la bibliothèque est intacte, pas d'album partiel.
        assert not (config.library_path / 'Pink Floyd').exists()

    def test_pas_de_temporaire_residuel(self, disc, config, monkeypatch):
        _fake_rip(monkeypatch)
        rip_disc(disc, config, ENCODERS['flac'])
        assert not (config.library_path / '.jewelbox-music-ripper-tmp').exists()

    def test_progression_rapportee(self, disc, config, monkeypatch):
        _fake_rip(monkeypatch)
        vus = []
        rip_disc(disc, config, ENCODERS['flac'],
                 progress=lambda i, t, label: vus.append((i, t)))

        assert vus[0] == (1, 2)
        assert vus[-1] == (2, 2)
