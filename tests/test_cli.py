"""Saisie interactive de la CLI.

`cli` est exclu de la mesure de couverture (il pilote un lecteur optique et un
terminal), mais la saisie manuelle est de la logique pure : elle se teste en
remplaçant `input`, et elle mérite de l'être — c'est le dernier recours quand
un disque n'est identifié nulle part.
"""

import pytest

from jewelbox_ripper.cli import _manual_disc, _prompt


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
