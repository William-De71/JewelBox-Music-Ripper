"""Saisie interactive de la CLI.

`cli` est exclu de la mesure de couverture (il pilote un lecteur optique et un
terminal), mais la saisie manuelle est de la logique pure : elle se teste en
remplaçant `input`, et elle mérite de l'être — c'est le dernier recours quand
un disque n'est identifié nulle part.
"""

import pytest

from jewelbox_ripper.api import ApiError
from jewelbox_ripper.cli import (
    _add_missing_disc,
    _discogs_credentials,
    _manual_disc,
    _prompt,
    format_progress_line,
)
from jewelbox_ripper.config import Config
from jewelbox_ripper.disc import DiscInfo, TrackInfo


class TestFormatProgressLine:
    def test_barre_et_pourcentage(self):
        line = format_progress_line('  [ 3/12] Titre', 0.5, 'Lecture', 83, 100)
        assert '█' * 10 + '░' * 10 in line
        assert '50 %' in line
        assert 'Lecture  1:23' in line
        assert line.startswith('  [ 3/12] Titre')

    def test_duree_inconnue_sans_barre(self):
        line = format_progress_line('  [ 1/2] T', None, 'Lecture', 5, 80)
        assert '%' not in line
        assert '█' not in line
        assert 'Lecture  0:05' in line

    def test_ne_deborde_jamais_du_terminal(self):
        # Une ligne trop large passerait à la ligne et casserait le \r.
        titre = '  [ 1/2] ' + 'très long titre ' * 20
        for width in (20, 40, 80, 200):
            line = format_progress_line(titre, 0.42, 'Relecture rapide', 600, width)
            assert len(line) == width - 1

    def test_titre_tronque_avant_la_barre(self):
        titre = '  [ 1/2] ' + 'x' * 200
        line = format_progress_line(titre, 1.0, 'Lecture', 0, 80)
        assert '100 %' in line
        assert 'Lecture  0:00' in line


def saisies(monkeypatch, reponses):
    """Remplace input() par une suite de réponses.

    Une réponse None simule un Ctrl+D (fin de flux), que Python signale par
    EOFError.
    """
    iterateur = iter(reponses)

    def fake_input(label=''):
        try:
            valeur = next(iterateur)
        except StopIteration:
            raise EOFError from None
        if valeur is None:
            raise EOFError
        return valeur

    monkeypatch.setattr('builtins.input', fake_input)


class TestPrompt:
    def test_reponse_normale(self, monkeypatch):
        saisies(monkeypatch, ['  Nirvana  '])
        assert _prompt('  Artiste : ') == 'Nirvana'

    def test_ctrl_d_vaut_reponse_vide(self, monkeypatch):
        # Sans ça, l'EOFError remonterait jusqu'au sommet et afficherait une
        # trace Python au lieu d'une sortie propre.
        saisies(monkeypatch, [None])
        assert _prompt('  Artiste : ') == ''

    def test_ligne_vide(self, monkeypatch):
        saisies(monkeypatch, [''])
        assert _prompt('  Artiste : ') == ''


class TestSaisieManuelle:
    def test_saisie_complete(self, monkeypatch):
        saisies(monkeypatch, ['Nirvana', 'Nevermind', '1991', 'Rock',
                              'Smells Like Teen Spirit', 'In Bloom'])
        disc = _manual_disc(2)

        assert disc.artist == 'Nirvana'
        assert disc.album == 'Nevermind'
        assert disc.year == 1991
        assert disc.genre == 'Rock'
        assert [t.title for t in disc.tracks] == ['Smells Like Teen Spirit',
                                                  'In Bloom']
        assert [t.number for t in disc.tracks] == [1, 2]

    def test_annulation_sur_artiste_vide(self, monkeypatch):
        saisies(monkeypatch, [''])
        assert _manual_disc(3) is None

    def test_annulation_sur_album_vide(self, monkeypatch):
        saisies(monkeypatch, ['Nirvana', ''])
        assert _manual_disc(3) is None

    def test_annee_non_numerique_ignoree(self, monkeypatch):
        saisies(monkeypatch, ['A', 'B', 'inconnue', '', 'Un'])
        assert _manual_disc(1).year is None

    def test_champs_facultatifs_vides(self, monkeypatch):
        saisies(monkeypatch, ['A', 'B', '', '', 'Un'])
        disc = _manual_disc(1)
        assert disc.year is None
        assert disc.genre is None

    def test_titres_vides_numerotes(self, monkeypatch):
        saisies(monkeypatch, ['A', 'B', '', '', '', 'Deux', ''])
        assert [t.title for t in _manual_disc(3).tracks] == \
            ['Piste 1', 'Deux', 'Piste 3']


class TestInterruptionParCtrlD:
    """Ctrl+D ne doit jamais faire remonter d'EOFError : selon le moment, il
    annule la saisie ou valide ce qui a déjà été fourni."""

    def test_ctrl_d_sur_artiste_annule(self, monkeypatch):
        saisies(monkeypatch, [None])
        assert _manual_disc(3) is None

    def test_ctrl_d_sur_album_annule(self, monkeypatch):
        saisies(monkeypatch, ['Nirvana', None])
        assert _manual_disc(3) is None

    def test_ctrl_d_sur_annee_poursuit(self, monkeypatch):
        # Champ facultatif : rien ne justifie de jeter l'artiste et l'album
        # déjà saisis.
        saisies(monkeypatch, ['A', 'B', None, '', 'Un'])
        disc = _manual_disc(1)
        assert disc is not None
        assert disc.year is None

    def test_ctrl_d_sur_les_titres_complete(self, monkeypatch):
        # Artiste et album sont acquis : les titres restants prennent leur
        # valeur par défaut plutôt que de tout perdre.
        saisies(monkeypatch, ['A', 'B', '1990', 'Rock', 'Un', None])
        disc = _manual_disc(3)

        assert disc is not None
        assert [t.title for t in disc.tracks] == ['Un', 'Piste 2', 'Piste 3']
        assert disc.year == 1990

    def test_flux_epuise_ne_leve_pas(self, monkeypatch):
        # Entrée redirigée depuis /dev/null : chaque lecture lève EOFError.
        saisies(monkeypatch, [])
        assert _manual_disc(3) is None

    def test_disque_complet_malgre_ctrl_d_immediat_sur_titres(self, monkeypatch):
        saisies(monkeypatch, ['A', 'B', '', '', None])
        disc = _manual_disc(4)

        assert disc is not None
        assert len(disc.tracks) == 4
        assert all(t.title.startswith('Piste ') for t in disc.tracks)


class TestCascadeIdentifiantsDiscogs:
    """Le serveur d'abord, la configuration locale ensuite : reprendre les
    identifiants du serveur évite d'en créer un second jeu."""

    class FakeApi:
        def __init__(self, pair=None):
            self._pair = pair

        def discogs_credentials(self):
            return self._pair

    def test_serveur_prioritaire(self):
        config = Config(library_path='/tmp', discogs_token='jeton-local')
        creds = _discogs_credentials(config, self.FakeApi(('cle', 'secret')))

        assert creds.key == 'cle'
        assert creds.token is None
        assert 'serveur' in creds.source

    def test_repli_sur_le_jeton_local(self):
        config = Config(library_path='/tmp', discogs_token='jeton-local')
        creds = _discogs_credentials(config, self.FakeApi(None))

        assert creds.token == 'jeton-local'
        assert 'ripper' in creds.source

    def test_sans_serveur(self):
        # Serveur injoignable : l'API n'est pas transmise du tout.
        config = Config(library_path='/tmp', discogs_token='jeton-local')
        assert _discogs_credentials(config, None).token == 'jeton-local'

    def test_aucune_source(self):
        config = Config(library_path='/tmp', discogs_token=None)
        assert _discogs_credentials(config, self.FakeApi(None)) is None

    def test_sans_configuration(self):
        assert _discogs_credentials(None, self.FakeApi(None)) is None

    def test_serveur_seul_suffit(self):
        # Aucun jeton local : les identifiants du serveur font tout le travail.
        config = Config(library_path='/tmp', discogs_token=None)
        creds = _discogs_credentials(config, self.FakeApi(('cle', 'secret')))
        assert creds.valid


class TestAddMissingDisc:
    """Le deuxième CD d'un coffret complète l'album créé par le premier."""

    class FakeApi:
        def __init__(self, tracks, fail=False):
            self.tracks = tracks
            self.fail = fail
            self.updates = []

        def get_album(self, album_id):
            if self.fail:
                raise ApiError('panne')
            return {'id': album_id, 'tracks': self.tracks}

        def update_album(self, album_id, changes):
            self.updates.append((album_id, changes))
            return {}

    CD1 = [
        {'id': 1, 'disc': 1, 'position': 1, 'title': 'In the Flesh?',
         'duration': '3:19', 'has_file': True},
        {'id': 2, 'disc': 1, 'position': 2, 'title': 'The Thin Ice',
         'duration': None},
    ]

    @staticmethod
    def _cd2():
        return DiscInfo(
            artist='Pink Floyd', album='The Wall', disc_number=2, disc_total=2,
            tracks=[TrackInfo(1, 'Hey You', 280.0), TrackInfo(2, 'Vera')],
        )

    def _run(self, api, disc=None, **kwargs):
        options = {'dry_run': False, 'assume_yes': True, **kwargs}
        _add_missing_disc(api, {'id': 7}, disc or self._cd2(), **options)

    def test_ajoute_les_pistes_en_gardant_les_existantes(self):
        api = self.FakeApi(self.CD1)
        self._run(api)

        (album_id, changes), = api.updates
        assert album_id == 7
        assert changes['tracks'] == [
            {'disc': 1, 'position': 1, 'title': 'In the Flesh?', 'duration': '3:19'},
            {'disc': 1, 'position': 2, 'title': 'The Thin Ice'},
            {'position': 1, 'title': 'Hey You', 'duration': '4:40', 'disc': 2},
            {'position': 2, 'title': 'Vera', 'disc': 2},
        ]

    def test_cd_deja_present(self):
        api = self.FakeApi(self.CD1 + [
            {'id': 3, 'disc': 2, 'position': 1, 'title': 'Hey You'}])
        self._run(api)
        assert api.updates == []

    def test_pistes_deja_saisies_sur_un_autre_cd(self):
        # Coffret saisi à plat avant la gestion des CD : pas de doublons.
        api = self.FakeApi(self.CD1 + [
            {'id': 3, 'position': 3, 'title': 'hey you'},
            {'id': 4, 'position': 4, 'title': 'Vera'}])
        self._run(api)
        assert api.updates == []

    def test_dry_run_ne_modifie_rien(self):
        api = self.FakeApi(self.CD1)
        self._run(api, dry_run=True)
        assert api.updates == []

    def test_refus_de_l_utilisateur(self, monkeypatch):
        monkeypatch.setattr('jewelbox_ripper.cli._confirm',
                            lambda *args, **kwargs: False)
        api = self.FakeApi(self.CD1)
        self._run(api, assume_yes=False)
        assert api.updates == []

    def test_fiche_illisible(self):
        api = self.FakeApi(self.CD1, fail=True)
        self._run(api)
        assert api.updates == []
