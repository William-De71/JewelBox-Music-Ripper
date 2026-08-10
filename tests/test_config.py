"""Configuration : fusion fichier/CLI et validation de la bibliothèque."""

import pytest

from jewelbox_ripper.config import Config, ConfigError, build_config, load_config


class TestBuildConfig:
    def test_valeurs_par_defaut(self):
        config = build_config({'library_path': '/musique'}, {})
        assert str(config.library_path) == '/musique'
        assert config.server_url == 'http://localhost:3001'
        assert config.audio_format == 'flac'
        assert config.paranoia is True

    def test_la_cli_prime_sur_le_fichier(self):
        config = build_config(
            {'library_path': '/a', 'audio_format': 'flac'},
            {'library_path': '/b', 'audio_format': 'opus'},
        )
        assert str(config.library_path) == '/b'
        assert config.audio_format == 'opus'

    def test_bibliotheque_requise(self):
        with pytest.raises(ConfigError) as excinfo:
            build_config({}, {})

        # Le message doit être actionnable : la première exécution se fait
        # forcément sans configuration.
        message = str(excinfo.value)
        assert 'make config' in message
        assert 'library_path' in message

    def test_format_en_minuscules(self):
        config = build_config({'library_path': '/m', 'audio_format': 'FLAC'}, {})
        assert config.audio_format == 'flac'

    def test_slash_final_retire(self):
        config = build_config(
            {'library_path': '/m', 'server_url': 'http://x:3001/'}, {})
        assert config.server_url == 'http://x:3001'

    def test_tilde_developpe(self):
        config = build_config({'library_path': '~/musique'}, {})
        assert '~' not in str(config.library_path)


class TestValidate:
    def test_bibliotheque_valide(self, tmp_path):
        Config(library_path=tmp_path).validate()

    def test_bibliotheque_absente(self, tmp_path):
        config = Config(library_path=tmp_path / 'nexistepas')
        with pytest.raises(ConfigError, match='introuvable'):
            config.validate()

    def test_bibliotheque_est_un_fichier(self, tmp_path):
        fichier = tmp_path / 'fichier.txt'
        fichier.write_text('x')
        with pytest.raises(ConfigError, match="n'est pas un dossier"):
            Config(library_path=fichier).validate()

    def test_bibliotheque_en_lecture_seule(self, tmp_path):
        lecture_seule = tmp_path / 'ro'
        lecture_seule.mkdir()
        lecture_seule.chmod(0o500)
        try:
            with pytest.raises(ConfigError, match='lecture seule'):
                Config(library_path=lecture_seule).validate()
        finally:
            lecture_seule.chmod(0o700)


class TestLoadConfig:
    def test_fichier_absent_non_requis(self, tmp_path):
        assert load_config(tmp_path / 'absent.toml') == {}

    def test_fichier_absent_requis(self, tmp_path):
        with pytest.raises(ConfigError, match='introuvable'):
            load_config(tmp_path / 'absent.toml', required=True)

    def test_lecture_toml(self, tmp_path):
        config_file = tmp_path / 'config.toml'
        config_file.write_text(
            'library_path = "/musique"\nserver_url = "http://nas:3001"\n')
        data = load_config(config_file)
        assert data['library_path'] == '/musique'
        assert data['server_url'] == 'http://nas:3001'

    def test_toml_invalide(self, tmp_path):
        config_file = tmp_path / 'config.toml'
        config_file.write_text('library_path = [invalide')
        with pytest.raises(ConfigError, match='TOML invalide'):
            load_config(config_file)
