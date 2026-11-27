"""Extraction et encodage : construction des commandes et gestion des échecs.

Les binaires externes (cdparanoia, flac, opusenc, lame) sont remplacés par des
doubles : on vérifie les arguments passés et le traitement des codes de retour,
pas le comportement des outils eux-mêmes.
"""

import subprocess
import threading
from pathlib import Path

import pytest

from jewelbox_ripper.disc import TrackInfo
from jewelbox_ripper.ripper import (
    ENCODERS,
    DiskFullError,
    EncoderSpec,
    MissingToolError,
    RipError,
    RipTimeoutError,
    eject,
    encode,
    find_cdparanoia,
    resolve_encoder,
    rip_and_encode,
    required_free_bytes,
    rip_budget,
    rip_track,
    verify_encoded,
    wav_fraction,
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
            # En-tête WAV (44 octets) suivi d'un peu d'audio.
            target.write_bytes(b'\0' * 44 + b'donnees-simulees')
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

    def test_en_tete_sans_audio_refuse(self, tmp_path, monkeypatch, cdparanoia):
        # Disque plein : cdparanoia n'écrit que l'en-tête et sort sans erreur.
        wav = tmp_path / 't.wav'

        def fake_run(command, **kwargs):
            wav.write_bytes(b'\0' * 44)
            return FakeCompleted(0)

        monkeypatch.setattr(subprocess, 'run', fake_run)
        with pytest.raises(DiskFullError, match='aucune donnée audio'):
            rip_track(3, wav)

    def test_binaire_illancable(self, tmp_path, monkeypatch, cdparanoia):
        def boom(*a, **kw):
            raise OSError('permission refusée')

        monkeypatch.setattr(subprocess, 'run', boom)
        with pytest.raises(RipError, match='impossible'):
            rip_track(1, tmp_path / 'o.wav')

    def test_delai_depasse(self, tmp_path, monkeypatch, cdparanoia):
        seen = {}

        def too_slow(command, **kwargs):
            seen.update(kwargs)
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])

        monkeypatch.setattr(subprocess, 'run', too_slow)

        with pytest.raises(RipTimeoutError, match='piste 4'):
            rip_track(4, tmp_path / 'o.wav', timeout=600)

        assert seen['timeout'] == 600


class TestWavFraction:
    def test_moitie_de_piste(self):
        assert wav_fraction(44 + 176_400, 2.0) == 0.5

    def test_en_tete_seul(self):
        assert wav_fraction(44, 100.0) == 0.0

    def test_fichier_plus_court_que_l_en_tete(self):
        assert wav_fraction(0, 100.0) == 0.0

    def test_plafonne_a_cent_pour_cent(self):
        # La durée vient des métadonnées : elle peut être un peu sous-estimée.
        assert wav_fraction(44 + 176_400 * 3, 2.0) == 1.0

    def test_duree_inconnue(self):
        assert wav_fraction(5000, None) is None


class TestRipBudget:
    def test_proportionnel_a_la_duree(self):
        assert rip_budget(600) == 1200

    def test_minimum_pour_les_pistes_courtes(self):
        assert rip_budget(30) == 300

    def test_duree_inconnue(self):
        assert rip_budget(None) == 1800


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

    def _fake_tools(self, monkeypatch, rip_outcomes):
        """Chaque lecture suit `rip_outcomes` : True écrit le WAV, False dépasse le délai."""
        modes = []

        def fake_rip(number, destination, **kwargs):
            modes.append(kwargs['paranoia'])
            # Comme le vrai cdparanoia : un WAV partiel reste sur le disque.
            destination.write_bytes(b'RIFF')
            if not rip_outcomes[len(modes) - 1]:
                raise RipTimeoutError('lecture trop lente')
            return destination

        def fake_encode(wav, output, spec, quality=None):
            output.write_bytes(b'fLaC')
            return output

        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track', fake_rip)
        monkeypatch.setattr('jewelbox_ripper.ripper.encode', fake_encode)
        return modes

    def test_relecture_rapide_apres_delai(self, tmp_path, monkeypatch):
        modes = self._fake_tools(monkeypatch, [False, True])
        degraded = []

        out = rip_and_encode(TrackInfo(3, 'T', 200.0), tmp_path / 'o.flac',
                             ENCODERS['flac'], workdir=tmp_path,
                             on_degraded=lambda: degraded.append(3))

        assert out.exists()
        assert modes == [True, False]
        assert degraded == [3]
        assert not list(tmp_path.glob('*.wav'))

    def test_lecture_normale_non_signalee(self, tmp_path, monkeypatch):
        modes = self._fake_tools(monkeypatch, [True])
        degraded = []

        rip_and_encode(TrackInfo(3, 'T', 200.0), tmp_path / 'o.flac',
                       ENCODERS['flac'], workdir=tmp_path,
                       on_degraded=lambda: degraded.append(3))

        assert modes == [True]
        assert degraded == []

    def test_echec_si_la_relecture_depasse_aussi(self, tmp_path, monkeypatch):
        # Le rip doit toujours finir par passer à la piste suivante.
        modes = self._fake_tools(monkeypatch, [False, False])
        degraded = []

        with pytest.raises(RipTimeoutError):
            rip_and_encode(TrackInfo(3, 'T', 200.0), tmp_path / 'o.flac',
                           ENCODERS['flac'], workdir=tmp_path,
                           on_degraded=lambda: degraded.append(3))

        assert modes == [True, False]
        assert degraded == []
        assert not list(tmp_path.glob('*.wav'))

    def test_etapes_rapportees(self, tmp_path, monkeypatch):
        self._fake_tools(monkeypatch, [False, True])
        vus = []

        rip_and_encode(TrackInfo(3, 'T', 200.0), tmp_path / 'o.flac',
                       ENCODERS['flac'], workdir=tmp_path,
                       on_progress=lambda f, phase: vus.append((f, phase)))

        assert vus == [(0.0, 'Lecture'), (0.0, 'Relecture rapide'),
                       (1.0, 'Encodage')]

    def test_avancement_suivi_pendant_la_lecture(self, tmp_path, monkeypatch):
        monkeypatch.setattr('jewelbox_ripper.ripper.PROGRESS_POLL_SECONDS', 0.001)
        vus = []
        seen_growth = threading.Event()

        def report(fraction, phase):
            vus.append(fraction)
            if fraction == 0.5:
                seen_growth.set()

        def fake_rip(number, destination, **kwargs):
            destination.write_bytes(b'\0' * (44 + 176_400))
            assert seen_growth.wait(5)
            return destination

        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track', fake_rip)
        monkeypatch.setattr('jewelbox_ripper.ripper.encode',
                            lambda wav, output, spec, quality=None: output)

        rip_and_encode(TrackInfo(1, 'T', 2.0), tmp_path / 'o.flac',
                       ENCODERS['flac'], workdir=tmp_path, on_progress=report)

        assert 0.5 in vus

    def test_avancement_sans_fichier_encore_cree(self, tmp_path, monkeypatch):
        monkeypatch.setattr('jewelbox_ripper.ripper.PROGRESS_POLL_SECONDS', 0.001)
        vus = threading.Semaphore(0)

        def fake_rip(number, destination, **kwargs):
            # Plusieurs relevés avant que cdparanoia ait créé le WAV.
            for _ in range(3):
                assert vus.acquire(timeout=5)
            destination.write_bytes(b'RIFF')
            return destination

        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track', fake_rip)
        monkeypatch.setattr('jewelbox_ripper.ripper.encode',
                            lambda wav, output, spec, quality=None: output)

        rip_and_encode(TrackInfo(1, 'T', 2.0), tmp_path / 'o.flac',
                       ENCODERS['flac'], workdir=tmp_path,
                       on_progress=lambda f, phase: vus.release())

    def test_mode_rapide_pas_de_seconde_lecture(self, tmp_path, monkeypatch):
        modes = self._fake_tools(monkeypatch, [False])

        with pytest.raises(RipTimeoutError):
            rip_and_encode(TrackInfo(3, 'T', 200.0), tmp_path / 'o.flac',
                           ENCODERS['flac'], workdir=tmp_path, paranoia=False)

        assert modes == [False]


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


class TestFreeSpace:
    def test_wav_et_sortie_plus_marge(self):
        # 10 s : 1 764 044 octets de WAV, comptés deux fois, plus 64 Mo.
        assert required_free_bytes(10) == 2 * 1_764_044 + 64 * 1024 * 1024

    def test_duree_inconnue_prevoit_large(self):
        assert required_free_bytes(None) == required_free_bytes(20 * 60)

    def _usage(self, monkeypatch, free):
        class Usage:
            pass
        usage = Usage()
        usage.free = free
        monkeypatch.setattr('shutil.disk_usage', lambda p: usage)

    def test_disque_plein_avant_lecture(self, tmp_path, monkeypatch):
        self._usage(monkeypatch, 10 * 1024 * 1024)
        lu = []
        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track',
                            lambda *a, **k: lu.append(1))

        with pytest.raises(DiskFullError, match='10 Mo libres'):
            rip_and_encode(TrackInfo(1, 'T', 200.0), tmp_path / 'o.flac',
                           ENCODERS['flac'], workdir=tmp_path)
        # Rien n'est lu : le CD n'est même pas sollicité.
        assert lu == []

    def test_dossier_de_sortie_aussi_verifie(self, tmp_path, monkeypatch):
        work, out_dir = tmp_path / 'w', tmp_path / 'o'
        work.mkdir()
        out_dir.mkdir()

        class Usage:
            def __init__(self, free):
                self.free = free

        monkeypatch.setattr(
            'shutil.disk_usage',
            lambda p: Usage(10**12 if Path(p) == work else 1024))
        with pytest.raises(DiskFullError):
            rip_and_encode(TrackInfo(1, 'T', 200.0), out_dir / 'o.flac',
                           ENCODERS['flac'], workdir=work)

    def test_espace_inconnu_ne_bloque_pas(self, tmp_path, monkeypatch):
        def boom(p):
            raise OSError('statvfs indisponible')

        monkeypatch.setattr('shutil.disk_usage', boom)

        def fake_rip(number, destination, **kwargs):
            destination.write_bytes(b'RIFF')
            return destination

        def fake_encode(wav, output, spec, quality=None):
            output.write_bytes(b'fLaC')
            return output

        monkeypatch.setattr('jewelbox_ripper.ripper.rip_track', fake_rip)
        monkeypatch.setattr('jewelbox_ripper.ripper.encode', fake_encode)
        out = rip_and_encode(TrackInfo(1, 'T', 200.0), tmp_path / 'o.flac',
                             ENCODERS['flac'], workdir=tmp_path)
        assert out.exists()


class TestVerifyEncoded:
    def test_flac_relu(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run(command, **kwargs):
            seen['command'] = command
            return FakeCompleted(0)

        monkeypatch.setattr(subprocess, 'run', fake_run)
        out = tmp_path / 'o.flac'
        verify_encoded(out, ENCODERS['flac'])
        assert seen['command'] == ['flac', '--test', '--silent', str(out)]

    def test_flac_tronque_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, 'run',
            lambda c, **k: FakeCompleted(1, stderr='unexpected end of file'))
        with pytest.raises(DiskFullError, match='incomplet'):
            verify_encoded(tmp_path / 'o.flac', ENCODERS['flac'])

    def test_verification_illancable(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise OSError('introuvable')

        monkeypatch.setattr(subprocess, 'run', boom)
        with pytest.raises(RipError, match='impossible'):
            verify_encoded(tmp_path / 'o.flac', ENCODERS['flac'])

    def test_formats_avec_perte_non_relus(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError('ne doit pas être appelé')

        monkeypatch.setattr(subprocess, 'run', boom)
        verify_encoded(tmp_path / 'o.opus', ENCODERS['opus'])
        verify_encoded(tmp_path / 'o.mp3', ENCODERS['mp3'])

    def test_encode_refuse_un_flac_tronque(self, tmp_path, monkeypatch):
        wav = tmp_path / 'in.wav'
        wav.write_bytes(b'RIFF')
        out = tmp_path / 'o.flac'

        def fake_run(command, **kwargs):
            if '--test' in command:
                return FakeCompleted(1, stderr='unexpected end of file')
            out.write_bytes(b'fLaC')
            return FakeCompleted(0)

        monkeypatch.setattr(subprocess, 'run', fake_run)
        with pytest.raises(DiskFullError):
            encode(wav, out, ENCODERS['flac'])
