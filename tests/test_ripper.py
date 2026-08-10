"""Extraction et encodage : construction des commandes et gestion des échecs.

Les binaires externes (cdparanoia, flac, opusenc, lame) sont remplacés par des
doubles : on vérifie les arguments passés et le traitement des codes de retour,
pas le comportement des outils eux-mêmes.
"""

import subprocess
from pathlib import Path

import pytest

from jewelbox_ripper.disc import TrackInfo
from jewelbox_ripper.ripper import (
    ENCODERS,
    EncoderSpec,
    MissingToolError,
    RipError,
    eject,
    encode,
    find_cdparanoia,
    resolve_encoder,
    rip_and_encode,
    rip_track,
)


class FakeCompleted:
    """Résultat de subprocess.run, réduit à ce que le code lit."""

    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_writing(monkeypatch, target: Path, *, returncode=0, stderr=''):
    """Double de subprocess.run qui crée le fichier attendu.

    Renvoie un dict qui recevra la commande vue, pour l'inspecter ensuite.
    """
    seen = {}

    def fake_run(command, **kwargs):
        seen['command'] = command
        if returncode == 0:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'donnees-simulees')
        return FakeCompleted(returncode, stderr=stderr)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    return seen


@pytest.fixture
def cdparanoia(monkeypatch):
    """Fait comme si cdparanoia était installé."""
    monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/cdparanoia')


class TestEncoderCommands:
    def test_flac_qualite_par_defaut(self):
        cmd = ENCODERS['flac'].command(Path('/t/in.wav'), Path('/t/out.flac'), None)
        assert cmd[0] == 'flac'
        assert '-8' in cmd
        assert '/t/out.flac' in cmd

    def test_flac_qualite_explicite(self):
        cmd = ENCODERS['flac'].command(Path('/t/in.wav'), Path('/t/out.flac'), '5')
        assert '-5' in cmd

    def test_opus_debit(self):
        cmd = ENCODERS['opus'].command(Path('/t/in.wav'), Path('/t/o.opus'), '192')
        assert cmd[0] == 'opusenc'
        assert '--bitrate' in cmd
        assert '192' in cmd

    def test_opus_debit_par_defaut(self):
        cmd = ENCODERS['opus'].command(Path('/t/in.wav'), Path('/t/o.opus'), None)
        assert '192' in cmd

    def test_mp3_vbr(self):
        cmd = ENCODERS['mp3'].command(Path('/t/in.wav'), Path('/t/o.mp3'), '2')
        assert cmd[0] == 'lame'
        assert '-V' in cmd
        assert '2' in cmd

    def test_mp3_qualite_par_defaut(self):
        cmd = ENCODERS['mp3'].command(Path('/t/in.wav'), Path('/t/o.mp3'), None)
        assert '0' in cmd

    def test_encodeur_inconnu(self):
        bogus = EncoderSpec('xyz', '.xyz', 'xyz', False, 'hint')
        with pytest.raises(RipError, match='Encodeur inconnu'):
            bogus.command(Path('/t/a.wav'), Path('/t/b.xyz'), None)


class TestResolveEncoder:
    def test_format_connu(self, monkeypatch):
        monkeypatch.setattr('shutil.which', lambda name: f'/usr/bin/{name}')
        assert resolve_encoder('flac').name == 'flac'

    def test_insensible_a_la_casse(self, monkeypatch):
        monkeypatch.setattr('shutil.which', lambda name: f'/usr/bin/{name}')
        assert resolve_encoder('FLAC').name == 'flac'

    def test_format_inconnu(self):
        with pytest.raises(RipError, match='non pris en charge'):
            resolve_encoder('wma')

    def test_binaire_absent(self, monkeypatch):
        monkeypatch.setattr('shutil.which', lambda name: None)
        with pytest.raises(MissingToolError) as excinfo:
            resolve_encoder('opus')
        # Le message doit dire quoi installer.
        assert 'opus-tools' in str(excinfo.value)


class TestFindCdparanoia:
    def test_nom_fedora(self, monkeypatch):
        monkeypatch.setattr(
            'shutil.which',
            lambda n: '/usr/bin/cdparanoia' if n == 'cdparanoia' else None)
        assert find_cdparanoia() == '/usr/bin/cdparanoia'

    def test_nom_alternatif(self, monkeypatch):
        # Certaines distributions fournissent cd-paranoia.
        monkeypatch.setattr(
            'shutil.which',
            lambda n: '/usr/bin/cd-paranoia' if n == 'cd-paranoia' else None)
        assert find_cdparanoia() == '/usr/bin/cd-paranoia'

    def test_absent(self, monkeypatch):
        monkeypatch.setattr('shutil.which', lambda n: None)
        with pytest.raises(MissingToolError, match='cdparanoia'):
            find_cdparanoia()


class TestRipTrack:
    def test_commande_construite(self, tmp_path, monkeypatch, cdparanoia):
        out = tmp_path / 'out.wav'
        seen = _run_writing(monkeypatch, out)

        rip_track(5, out)

        assert '5' in seen['command']
        assert str(out) in seen['command']

    def test_device_transmis(self, tmp_path, monkeypatch, cdparanoia):
        out = tmp_path / 'o.wav'
        seen = _run_writing(monkeypatch, out)

        rip_track(1, out, device='/dev/sr1')

        assert '--force-cdrom-device' in seen['command']
        assert '/dev/sr1' in seen['command']

    def test_mode_rapide(self, tmp_path, monkeypatch, cdparanoia):
        out = tmp_path / 'o.wav'
        seen = _run_writing(monkeypatch, out)

        rip_track(1, out, paranoia=False)
        assert '--disable-paranoia' in seen['command']

    def test_paranoia_par_defaut(self, tmp_path, monkeypatch, cdparanoia):
        out = tmp_path / 'o.wav'
        seen = _run_writing(monkeypatch, out)

        rip_track(1, out)
        assert '--disable-paranoia' not in seen['command']

    def test_dossier_parent_cree(self, tmp_path, monkeypatch, cdparanoia):
        out = tmp_path / 'sous' / 'dossier' / 'o.wav'
        _run_writing(monkeypatch, out)

        rip_track(1, out)
        assert out.exists()

    def test_echec_remonte(self, tmp_path, monkeypatch, cdparanoia):
        out = tmp_path / 'o.wav'
        _run_writing(monkeypatch, out, returncode=1, stderr='secteur illisible')

        with pytest.raises(RipError, match='secteur illisible'):
            rip_track(3, out)

    def test_fichier_vide_refuse(self, tmp_path, monkeypatch, cdparanoia):
        # Code 0 mais rien d'écrit : un WAV vide passerait sinon inaperçu.
        monkeypatch.setattr(subprocess, 'run', lambda c, **k: FakeCompleted(0))

        with pytest.raises(RipError, match='vide ou absent'):
            rip_track(1, tmp_path / 'o.wav')

    def test_binaire_illancable(self, tmp_path, monkeypatch, cdparanoia):
        def boom(*a, **kw):
            raise OSError('permission refusée')

        monkeypatch.setattr(subprocess, 'run', boom)
        with pytest.raises(RipError, match='impossible'):
            rip_track(1, tmp_path / 'o.wav')


class TestEncode:
    def test_succes(self, tmp_path, monkeypatch):
        wav = tmp_path / 'in.wav'
        wav.write_bytes(b'RIFF')
        out = tmp_path / 'out.flac'
        _run_writing(monkeypatch, out)

        assert encode(wav, out, ENCODERS['flac']) == out

    def test_qualite_transmise(self, tmp_path, monkeypatch):
        wav = tmp_path / 'in.wav'
        wav.write_bytes(b'RIFF')
        out = tmp_path / 'out.opus'
        seen = _run_writing(monkeypatch, out)

        encode(wav, out, ENCODERS['opus'], '128')
        assert '128' in seen['command']

    def test_echec_remonte(self, tmp_path, monkeypatch):
        wav = tmp_path / 'in.wav'
        wav.write_bytes(b'RIFF')
        monkeypatch.setattr(subprocess, 'run',
                            lambda c, **k: FakeCompleted(1, stderr='flux corrompu'))

        with pytest.raises(RipError, match='flux corrompu'):
            encode(wav, tmp_path / 'o.flac', ENCODERS['flac'])

    def test_sortie_vide_refusee(self, tmp_path, monkeypatch):
        wav = tmp_path / 'in.wav'
        wav.write_bytes(b'RIFF')
        monkeypatch.setattr(subprocess, 'run', lambda c, **k: FakeCompleted(0))

        with pytest.raises(RipError, match='vide ou absent'):
            encode(wav, tmp_path / 'o.flac', ENCODERS['flac'])

    def test_binaire_illancable(self, tmp_path, monkeypatch):
        wav = tmp_path / 'in.wav'
        wav.write_bytes(b'RIFF')

        def boom(*a, **kw):
            raise OSError('introuvable')

        monkeypatch.setattr(subprocess, 'run', boom)
        with pytest.raises(RipError, match='impossible'):
            encode(wav, tmp_path / 'o.flac', ENCODERS['flac'])


class TestRipAndEncode:
    def test_wav_intermediaire_supprime(self, tmp_path, monkeypatch):
        out = tmp_path / '01 - Titre.flac'

        def fake_rip(number, destination, **kwargs):
            destination.write_bytes(b'RIFF')
            return destination

        def fake_encode(wav, output, spec, quality=None):
            output.write_bytes(b'fLaC')
            return output

        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track', fake_rip)
        monkeypatch.setattr('jewelbox_ripper.ripper.encode', fake_encode)

        rip_and_encode(TrackInfo(1, 'Titre'), out, ENCODERS['flac'],
                       workdir=tmp_path)

        assert out.exists()
        # Le WAV ne doit pas survivre : un disque entier en WAV saturerait.
        assert not list(tmp_path.glob('*.wav'))

    def test_wav_supprime_meme_si_encodage_echoue(self, tmp_path, monkeypatch):
        def fake_rip(number, destination, **kwargs):
            destination.write_bytes(b'RIFF')
            return destination

        def boom(*a, **kw):
            raise RipError('encodage impossible')

        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track', fake_rip)
        monkeypatch.setattr('jewelbox_ripper.ripper.encode', boom)

        with pytest.raises(RipError):
            rip_and_encode(TrackInfo(2, 'Titre'), tmp_path / 'o.flac',
                           ENCODERS['flac'], workdir=tmp_path)

        assert not list(tmp_path.glob('*.wav'))

    def test_options_transmises(self, tmp_path, monkeypatch):
        seen = {}

        def fake_rip(number, destination, **kwargs):
            seen.update(kwargs)
            destination.write_bytes(b'RIFF')
            return destination

        def fake_encode(wav, output, spec, quality=None):
            output.write_bytes(b'x')
            return output

        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track', fake_rip)
        monkeypatch.setattr('jewelbox_ripper.ripper.encode', fake_encode)

        rip_and_encode(TrackInfo(1, 'T'), tmp_path / 'o.flac', ENCODERS['flac'],
                       workdir=tmp_path, device='/dev/sr2', paranoia=False)

        assert seen['device'] == '/dev/sr2'
        assert seen['paranoia'] is False


class TestEject:
    def test_succes(self, monkeypatch):
        monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/eject')
        monkeypatch.setattr(subprocess, 'run', lambda c, **k: FakeCompleted(0))
        assert eject() is True

    def test_binaire_absent_non_bloquant(self, monkeypatch):
        # L'éjection est cosmétique : son absence ne doit jamais lever.
        monkeypatch.setattr('shutil.which', lambda n: None)
        assert eject() is False

    def test_echec_non_bloquant(self, monkeypatch):
        monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/eject')
        monkeypatch.setattr(subprocess, 'run', lambda c, **k: FakeCompleted(1))
        assert eject() is False

    def test_erreur_os_non_bloquante(self, monkeypatch):
        monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/eject')

        def boom(*a, **kw):
            raise OSError('plateau bloqué')

        monkeypatch.setattr(subprocess, 'run', boom)
        assert eject() is False

    def test_device_transmis(self, monkeypatch):
        seen = {}
        monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/eject')

        def fake_run(command, **kwargs):
            seen['command'] = command
            return FakeCompleted(0)

        monkeypatch.setattr(subprocess, 'run', fake_run)
        eject('/dev/sr1')
        assert '/dev/sr1' in seen['command']
