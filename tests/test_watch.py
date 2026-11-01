"""Surveillance du lecteur : détection d'insertion et décision prise."""

import sys

from jewelbox_ripper import watch as watch_module
from jewelbox_ripper.watch import (
    CDS_AUDIO, CDS_DISC_OK, CDROM_DISC_STATUS, CDROM_DRIVE_STATUS,
    InsertionDetector, Watcher, drive_has_audio_disc, notify, rip_command,
    terminal_command, watch)


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


def test_commande_interactive():
    assert rip_command('/dev/sr0') == [
        sys.executable, '-m', 'jewelbox_ripper', '--device', '/dev/sr0']


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


# -- notifications -------------------------------------------------------------


def test_notification(capsys):
    calls = []
    notify('CD inséré', 'Nirvana', which=lambda _: '/usr/bin/notify-send',
           run=lambda command, **kw: calls.append(command))
    assert calls[0][0] == '/usr/bin/notify-send'
    assert calls[0][-2:] == ['CD inséré', 'Nirvana']
    assert 'CD inséré Nirvana' in capsys.readouterr().out


def test_notification_sans_notify_send(capsys):
    notify('CD inséré', which=lambda _: None)
    assert capsys.readouterr().out.strip() == 'CD inséré'


def test_notification_qui_echoue_ne_casse_rien():
    def boom(command, **kw):
        raise OSError('absent')
    notify('CD inséré', which=lambda _: '/bin/notify-send', run=boom)


# -- décision -------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.popens, self.notes = [], []

    def popen(self, command, **kw):
        self.popens.append((command, kw))

    def notify(self, title, body=''):
        self.notes.append((title, body))


def _watcher(rec, which=lambda name: '/usr/bin/' + name):
    return Watcher('/dev/sr0', popen=rec.popen, which=which, notify=rec.notify)


def test_insertion_ouvre_un_terminal_interactif():
    rec = Recorder()
    _watcher(rec).on_inserted()
    command, kw = rec.popens[0]
    assert command[0] == '/usr/bin/xdg-terminal-exec'
    # Pas de --yes : le choix de l'édition se fait dans le terminal.
    assert '--yes' not in command[-1]
    assert kw['start_new_session'] is True
    assert rec.notes[0][0] == 'CD inséré'


def test_insertion_sans_terminal():
    rec = Recorder()
    _watcher(rec, which=lambda name: None).on_inserted()
    assert rec.popens == []
    assert 'lance « jewelbox-rip » à la main' in rec.notes[0][1]


def test_terminal_qui_ne_demarre_pas():
    rec = Recorder()

    def boom(command, **kw):
        raise OSError('refusé')

    watcher = _watcher(rec)
    watcher.popen = boom
    watcher.on_inserted()
    assert 'Terminal impossible à ouvrir' in rec.notes[-1][1]


def test_poll_ne_reagit_qu_a_l_insertion():
    rec = Recorder()
    states = iter([True, False, True])
    watcher = _watcher(rec)
    watcher.has_disc = lambda device: next(states)
    for _ in range(3):
        watcher.poll()
    assert len(rec.popens) == 1


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
