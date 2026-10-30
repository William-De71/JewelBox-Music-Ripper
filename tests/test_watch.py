"""Surveillance du lecteur : détection d'insertion et décision prise."""

import subprocess
import sys

from jewelbox_ripper import watch as watch_module
from jewelbox_ripper.identify import IdentificationError, ReleaseCandidate
from jewelbox_ripper.watch import (
    CDS_AUDIO, CDS_DISC_OK, CDROM_DISC_STATUS, CDROM_DRIVE_STATUS,
    InsertionDetector, Watcher, drive_has_audio_disc, exit_message,
    identify_disc, notify, rip_command, terminal_command, watch)


# -- détection -------------------------------------------------------------


def test_un_disque_deja_present_au_demarrage_ne_declenche_rien():
    detector = InsertionDetector()
    assert detector.update(True) is False
    assert detector.update(True) is False


def test_une_insertion_declenche_une_seule_fois():
    detector = InsertionDetector()
    assert [detector.update(v) for v in (False, False, True, True, False, True)] == [
        False, False, True, False, False, True]


def _fake_drive(monkeypatch, *, open_error=None, statuses=None, ioctl_error=None):
    closed = []

    def fake_open(path, flags):
        if open_error:
            raise open_error
        return 42

    def fake_ioctl(fd, request, arg):
        if ioctl_error:
            raise ioctl_error
        return statuses[request]

    monkeypatch.setattr(watch_module.os, 'open', fake_open)
    monkeypatch.setattr(watch_module.os, 'close', closed.append)
    import fcntl
    monkeypatch.setattr(fcntl, 'ioctl', fake_ioctl)
    return closed


def test_lecteur_avec_cd_audio(monkeypatch):
    closed = _fake_drive(monkeypatch, statuses={
        CDROM_DRIVE_STATUS: CDS_DISC_OK, CDROM_DISC_STATUS: CDS_AUDIO})
    assert drive_has_audio_disc('/dev/sr0') is True
    assert closed == [42]


def test_disque_de_donnees_ignore(monkeypatch):
    _fake_drive(monkeypatch, statuses={
        CDROM_DRIVE_STATUS: CDS_DISC_OK, CDROM_DISC_STATUS: 101})
    assert drive_has_audio_disc('/dev/sr0') is False


def test_lecteur_vide_ou_tiroir_ouvert(monkeypatch):
    _fake_drive(monkeypatch, statuses={CDROM_DRIVE_STATUS: 2})
    assert drive_has_audio_disc('/dev/sr0') is False


def test_lecteur_absent(monkeypatch):
    _fake_drive(monkeypatch, open_error=FileNotFoundError())
    assert drive_has_audio_disc('/dev/sr9') is False


def test_ioctl_refuse(monkeypatch):
    closed = _fake_drive(monkeypatch, ioctl_error=OSError())
    assert drive_has_audio_disc('/dev/sr0') is False
    assert closed == [42]


# -- commandes ---------------------------------------------------------------


def test_commande_automatique_et_interactive():
    base = [sys.executable, '-m', 'jewelbox_ripper', '--device', '/dev/sr0']
    assert rip_command('/dev/sr0', interactive=False) == base + ['--yes']
    assert rip_command('/dev/sr0', interactive=True) == base


def test_premier_terminal_disponible():
    found = {'gnome-terminal': '/usr/bin/gnome-terminal', 'xterm': '/usr/bin/xterm'}
    command = terminal_command(['jewelbox-rip', "l'album"], found.get)
    assert command[:4] == ['/usr/bin/gnome-terminal', '--', 'bash', '-c']
    # Les arguments sont protégés, et la fenêtre attend avant de se fermer.
    assert command[4].startswith("'jewelbox-rip' 'l'\\''album'; echo; read")


def test_terminal_prefere_de_l_utilisateur():
    command = terminal_command(['x'], lambda name: '/usr/bin/' + name)
    assert command[:2] == ['/usr/bin/xdg-terminal-exec', 'bash']


def test_aucun_terminal():
    assert terminal_command(['x'], lambda name: None) is None


def test_messages_de_sortie():
    assert exit_message(4) == 'serveur injoignable ou album refusé'
    assert exit_message(99) == 'échec (code 99)'


# -- notifications -------------------------------------------------------------


def test_notification(capsys):
    calls = []
    notify('CD extrait', 'Nirvana', which=lambda _: '/usr/bin/notify-send',
           run=lambda command, **kw: calls.append(command))
    assert calls[0][0] == '/usr/bin/notify-send'
    assert calls[0][-2:] == ['CD extrait', 'Nirvana']
    assert 'CD extrait Nirvana' in capsys.readouterr().out


def test_notification_sans_notify_send(capsys):
    notify('CD extrait', which=lambda _: None)
    assert capsys.readouterr().out.strip() == 'CD extrait'


def test_notification_qui_echoue_ne_casse_rien():
    def boom(command, **kw):
        raise OSError('absent')
    notify('CD extrait', which=lambda _: '/bin/notify-send', run=boom)


# -- décision -------------------------------------------------------------------


class Recorder:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.runs, self.popens, self.notes = [], [], []

    def run(self, command, **kw):
        self.runs.append(command)
        return subprocess.CompletedProcess(command, self.returncode)

    def popen(self, command, **kw):
        self.popens.append((command, kw))

    def notify(self, title, body=''):
        self.notes.append((title, body))


def _watcher(rec, summary, which=lambda name: '/usr/bin/' + name):
    return Watcher('/dev/sr0', identify=lambda device: summary,
                   run=rec.run, popen=rec.popen, which=which, notify=rec.notify)


def test_disque_reconnu_extrait_en_arriere_plan():
    rec = Recorder()
    _watcher(rec, 'Nirvana — Nevermind').on_inserted()
    assert rec.runs == [rip_command('/dev/sr0', interactive=False)]
    assert rec.notes == [('Extraction du CD', 'Nirvana — Nevermind'),
                         ('CD extrait', 'Nirvana — Nevermind')]
    assert rec.popens == []


def test_extraction_en_echec_le_dit():
    rec = Recorder(returncode=6)
    _watcher(rec, 'Nirvana — Nevermind').on_inserted()
    assert rec.notes[-1] == ('Extraction du CD : certaines pistes ont échoué',
                             'Nirvana — Nevermind')


def test_disque_inconnu_ouvre_un_terminal():
    rec = Recorder()
    _watcher(rec, None).on_inserted()
    assert rec.runs == []
    command, kw = rec.popens[0]
    assert command[0] == '/usr/bin/xdg-terminal-exec'
    assert kw['start_new_session'] is True
    assert rec.notes[0][0] == 'CD inconnu de MusicBrainz'


def test_disque_inconnu_sans_terminal():
    rec = Recorder()
    _watcher(rec, None, which=lambda name: None).on_inserted()
    assert rec.popens == []
    assert 'lance « jewelbox-rip » à la main' in rec.notes[0][1]


def test_terminal_qui_ne_demarre_pas():
    rec = Recorder()

    def boom(command, **kw):
        raise OSError('refusé')

    watcher = _watcher(rec, None)
    watcher.popen = boom
    watcher.on_inserted()
    assert 'Terminal impossible à ouvrir' in rec.notes[-1][1]


def test_poll_ne_reagit_qu_a_l_insertion():
    rec = Recorder()
    states = iter([True, False, True])
    watcher = _watcher(rec, 'Album')
    watcher.has_disc = lambda device: next(states)
    for _ in range(3):
        watcher.poll()
    assert len(rec.runs) == 1


# -- identification ----------------------------------------------------------------


def _candidate():
    return ReleaseCandidate('id', 'Nevermind', 'Nirvana', '1991', 'US', 'DGC',
                            None, 12, None, {})


def test_identification(monkeypatch):
    monkeypatch.setattr('jewelbox_ripper.identify.read_disc_id', lambda d: ('abc', 12))
    monkeypatch.setattr('jewelbox_ripper.identify.lookup_candidates',
                        lambda disc_id: [_candidate()])
    assert identify_disc('/dev/sr0').startswith('Nirvana — Nevermind')


def test_identification_disque_inconnu(monkeypatch):
    monkeypatch.setattr('jewelbox_ripper.identify.read_disc_id', lambda d: ('abc', 12))
    monkeypatch.setattr('jewelbox_ripper.identify.lookup_candidates', lambda disc_id: [])
    assert identify_disc('/dev/sr0') is None


def test_identification_impossible(monkeypatch, capsys):
    def boom(device):
        raise IdentificationError('MusicBrainz injoignable')
    monkeypatch.setattr('jewelbox_ripper.identify.read_disc_id', boom)
    assert identify_disc('/dev/sr0') is None
    assert 'MusicBrainz injoignable' in capsys.readouterr().out


def test_identification_par_defaut_utilisee(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(watch_module, 'identify_disc', lambda device: None)
    watcher = Watcher('/dev/sr0', run=rec.run, popen=rec.popen,
                      which=lambda name: None, notify=rec.notify)
    watcher.on_inserted()
    assert rec.notes[0][0] == 'CD inconnu de MusicBrainz'


# -- boucle ---------------------------------------------------------------------------


def test_boucle_de_surveillance(capsys):
    polls, sleeps = [], []
    turns = iter([True, True, False])

    class FakeWatcher:
        device = '/dev/sr1'

        def poll(self):
            polls.append(1)

    assert watch(None, sleep=sleeps.append, watcher=FakeWatcher(),
                 keep_going=lambda: next(turns)) == 0
    assert len(polls) == 2
    assert sleeps == [watch_module.POLL_SECONDS] * 2
    assert '/dev/sr1' in capsys.readouterr().out


def test_lecteur_par_defaut(capsys):
    assert watch(None, keep_going=lambda: False) == 0
    assert 'Surveillance de /dev/sr0' in capsys.readouterr().out
