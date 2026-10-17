"""Normalisation ReplayGain : commande rsgain et gestion des échecs."""

import subprocess
from pathlib import Path

from jewelbox_ripper.loudness import (
    ReplayGainResult, apply_replaygain, replaygain_command)

FILES = [Path('/a/01 - Un.flac'), Path('/a/02 - Deux.flac')]


def _run_returning(returncode, stdout='', stderr=''):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return run, calls


def test_commande_mode_album_ecriture_silencieuse():
    assert replaygain_command('/usr/bin/rsgain', FILES) == [
        '/usr/bin/rsgain', 'custom', '-a', '-s', 'i', '-q',
        '/a/01 - Un.flac', '/a/02 - Deux.flac']


def test_succes():
    run, calls = _run_returning(0)
    result = apply_replaygain(FILES, binary='rsgain', run=run)
    assert result == ReplayGainResult(True)
    # Un seul appel pour tout l'album : c'est ce qui donne le gain d'album.
    assert len(calls) == 1
    assert calls[0][0][-2:] == [str(f) for f in FILES]
    assert calls[0][1]['check'] is False


def test_rsgain_absent(monkeypatch):
    monkeypatch.setattr('jewelbox_ripper.loudness.shutil.which', lambda _: None)
    result = apply_replaygain(FILES)
    assert not result.applied
    assert 'rsgain absent' in result.message
    assert 'dnf install rsgain' in result.message


def test_rsgain_trouve_dans_le_path(monkeypatch):
    monkeypatch.setattr('jewelbox_ripper.loudness.shutil.which',
                        lambda name: f'/usr/bin/{name}')
    run, calls = _run_returning(0)
    assert apply_replaygain(FILES, run=run).applied
    assert calls[0][0][0] == '/usr/bin/rsgain'


def test_echec_rapporte_la_derniere_ligne():
    run, _ = _run_returning(1, stderr='scan...\nError: unsupported file')
    result = apply_replaygain(FILES, binary='rsgain', run=run)
    assert not result.applied
    assert result.message == 'rsgain a échoué : Error: unsupported file'


def test_echec_sans_sortie():
    run, _ = _run_returning(3)
    assert apply_replaygain(FILES, binary='rsgain', run=run).message \
        == 'rsgain a échoué : code 3'


def test_binaire_inexecutable():
    def run(command, **kwargs):
        raise PermissionError('permission refusée')

    result = apply_replaygain(FILES, binary='rsgain', run=run)
    assert not result.applied
    assert 'permission refusée' in result.message


def test_aucun_fichier():
    assert not apply_replaygain([], binary='rsgain').applied
