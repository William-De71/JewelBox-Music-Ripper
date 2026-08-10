"""Le nommage doit rester compatible avec le scanner du serveur.

Les attentes reproduisent audioMatch.js (parsePathFallback, normalize) et
audioScanner.js (walkAudioFiles, listFolderAudioFiles).
"""

import re

import pytest

from jewelbox_ripper.naming import (
    MAX_COMPONENT,
    album_relpath,
    sanitize_component,
    track_filename,
)

# La regex exacte de parsePathFallback côté serveur.
SERVER_TRACK_RE = re.compile(r'^(\d{1,3})[\s.\-_]*(.*)$')


class TestSanitizeComponent:
    def test_conserve_les_accents(self):
        # normalize() du serveur retire les diacritiques : « Björk » et
        # « Bjork » s'apparient, donc rien ne justifie de les mutiler ici.
        assert sanitize_component('Björk') == 'Björk'
        assert sanitize_component('Édith Piaf') == 'Édith Piaf'

    def test_remplace_les_caracteres_interdits(self):
        assert sanitize_component('AC/DC') == 'AC-DC'
        assert sanitize_component('Where Are We Now?') == 'Where Are We Now-'

    def test_supprime_les_caracteres_de_controle(self):
        assert sanitize_component('Album\x07titre') == 'Albumtitre'

    def test_retire_le_point_initial(self):
        # walkAudioFiles ignore toute entrée commençant par « . ».
        assert not sanitize_component('.hidden').startswith('.')

    def test_retire_les_points_et_espaces_finaux(self):
        assert sanitize_component('Album.') == 'Album'
        assert sanitize_component('Album   ') == 'Album'

    def test_resserre_les_espaces(self):
        assert sanitize_component('Trop   d espaces') == 'Trop d espaces'

    def test_replie_sur_la_valeur_par_defaut(self):
        assert sanitize_component('') == 'Sans titre'
        assert sanitize_component('...') == 'Sans titre'
        assert sanitize_component('   ', fallback='Vide') == 'Vide'

    def test_prefixe_les_noms_reserves(self):
        assert sanitize_component('CON') == '_CON'
        assert sanitize_component('NUL') == '_NUL'

    def test_tronque_sans_couper_un_caractere(self):
        result = sanitize_component('é' * 300)
        assert len(result.encode('utf-8')) <= MAX_COMPONENT
        # Décodable : aucun caractère coupé en son milieu.
        result.encode('utf-8').decode('utf-8')


class TestTrackFilename:
    def test_format_standard(self):
        assert track_filename(3, 'Titre', '.flac') == '03 - Titre.flac'

    def test_relisible_par_le_serveur(self):
        name = track_filename(7, 'Comfortably Numb', '.flac')
        match = SERVER_TRACK_RE.match(name.removesuffix('.flac'))
        assert match is not None
        assert int(match.group(1)) == 7
        assert match.group(2) == 'Comfortably Numb'

    def test_trois_chiffres_au_dela_de_99(self):
        # La regex serveur accepte jusqu'à 3 chiffres.
        name = track_filename(101, 'Titre', '.flac')
        assert name.startswith('101 - ')
        assert SERVER_TRACK_RE.match(name.removesuffix('.flac'))

    def test_sans_numero(self):
        assert track_filename(None, 'Titre', '.flac') == 'Titre.flac'

    def test_extension_sans_point(self):
        assert track_filename(1, 'Titre', 'opus') == '01 - Titre.opus'

    def test_titre_tres_long_reste_valide(self):
        name = track_filename(5, 'x' * 400, '.flac')
        assert len(name.encode('utf-8')) <= MAX_COMPONENT
        assert name.endswith('.flac')
        assert name.startswith('05 - ')

    @pytest.mark.parametrize('title', ['AC/DC', 'A:B', 'x' * 300, 'é' * 200])
    def test_jamais_de_separateur_dans_le_nom(self, title):
        assert '/' not in track_filename(1, title, '.flac')


class TestAlbumRelpath:
    def test_deux_niveaux_pour_un_disque_unique(self):
        # parsePathFallback attend « Artiste/Album/piste » : exactement deux
        # niveaux avant le fichier.
        assert album_relpath('Pink Floyd', 'The Wall') == 'Pink Floyd/The Wall'

    def test_pas_de_sous_dossier_si_un_seul_disque(self):
        path = album_relpath('A', 'B', disc=1, disc_total=1)
        assert path == 'A/B'

    def test_sous_dossier_cd_pour_un_coffret(self):
        path = album_relpath('Pink Floyd', 'The Wall', disc=2, disc_total=2)
        assert path == 'Pink Floyd/The Wall/CD2'

    def test_repli_sur_artiste_inconnu(self):
        assert album_relpath('', 'Album') == 'Artiste inconnu/Album'
        assert album_relpath('Artiste', '') == 'Artiste/Album inconnu'

    def test_assainit_chaque_composant(self):
        assert album_relpath('AC/DC', 'Back in Black') == 'AC-DC/Back in Black'

    def test_profondeur_compatible_avec_le_repli_serveur(self):
        # Le repli lit parts[-3] comme artiste et parts[-2] comme album.
        full = f"{album_relpath('Pink Floyd', 'Animals')}/01 - Pigs.flac"
        parts = full.split('/')
        assert parts[-3] == 'Pink Floyd'
        assert parts[-2] == 'Animals'
