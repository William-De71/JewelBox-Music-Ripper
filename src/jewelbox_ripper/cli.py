"""Interface en ligne de commande du ripper.

Déroulé d'une exécution :

  1. lire le Disc ID du CD inséré ;
  2. interroger MusicBrainz, faire choisir l'édition — à défaut, chercher sur
     Discogs, puis en dernier recours saisir à la main ;
  3. vérifier la présence de l'album dans la collection JewelBox ;
     - absent  → proposer de le créer via l'API ;
     - présent et « souhaité » → proposer de le marquer possédé ;
  4. extraire, encoder, taguer, publier ;
  5. déclencher le scan du serveur et rendre compte de l'appariement.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from typing import Any
from pathlib import Path

from . import __version__
from .api import AlbumConflict, AlbumPayload, ApiError, JewelBoxApi, TrackPayload
from .config import Config, ConfigError, build_config, default_config_path, load_config
from .disc import DiscInfo, TrackInfo, format_duration
from .discogs import (
    DiscogsClient,
    DiscogsCredentials,
    DiscogsError,
    DiscogsRelease,
)
from .discogs import build_disc_info as build_discogs_disc_info
from .identify import (
    DiscReadError,
    IdentificationError,
    ReleaseCandidate,
    build_disc_info,
    lookup_candidates,
    read_disc_id,
)
from .pipeline import PipelineError, plan_paths, rip_disc
from .ripper import MissingToolError, RipError, eject, resolve_encoder
from .watch import watch

BAR_WIDTH = 20


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = _resolve_config(args)
    except ConfigError as exc:
        _error(str(exc))
        return 2

    try:
        if args.watch:
            return watch(config.device)
        return _run(args, config)
    except KeyboardInterrupt:
        _warn('\nInterrompu. Rien n\'a été laissé dans la bibliothèque.')
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='jewelbox-rip',
        description='Extrait un CD audio vers la bibliothèque JewelBox Music.',
    )
    parser.add_argument('--version', action='version',
                        version=f'jewelbox-music-ripper {__version__}')
    parser.add_argument('-c', '--config', type=Path,
                        help=f'fichier de configuration (défaut : {default_config_path()})')
    parser.add_argument('-l', '--library', type=Path,
                        help='racine de la bibliothèque musicale du serveur')
    parser.add_argument('-s', '--server',
                        help='URL du serveur JewelBox (ex. http://localhost:3001)')
    parser.add_argument('-f', '--format', choices=('flac', 'opus', 'mp3'),
                        help='format de sortie (défaut : flac)')
    parser.add_argument('-q', '--quality',
                        help='qualité : 0-8 en FLAC, débit kbps en Opus, 0-9 en MP3')
    parser.add_argument('-d', '--device',
                        help='lecteur optique (ex. /dev/sr0)')
    parser.add_argument('--fast', action='store_true',
                        help='désactive la correction d\'erreurs (rapide, moins sûr)')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='affiche ce qui serait fait, sans rien écrire')
    parser.add_argument('-y', '--yes', action='store_true',
                        help='accepte les questions par défaut (non interactif)')
    parser.add_argument('--no-scan', action='store_true',
                        help='ne pas lancer le scan du serveur après le rip')
    parser.add_argument('--no-eject', action='store_true',
                        help='ne pas éjecter le disque à la fin')
    parser.add_argument('--no-replaygain', action='store_true',
                        help='ne pas calculer les tags ReplayGain (rsgain)')
    parser.add_argument('--watch', action='store_true',
                        help='surveille le lecteur et extrait chaque CD inséré '
                             '(service : make watch-install)')
    return parser


def _resolve_config(args: argparse.Namespace) -> Config:
    file_data = load_config(args.config, required=args.config is not None)
    overrides = {
        'library_path': str(args.library) if args.library else None,
        'server_url': args.server,
        'audio_format': args.format,
        'quality': args.quality,
        'device': args.device,
        'paranoia': False if args.fast else None,
        'replaygain': False if args.no_replaygain else None,
    }
    return build_config(file_data, {k: v for k, v in overrides.items()
                                    if v is not None})


def _run(args: argparse.Namespace, config: Config) -> int:
    # Tout ce qui peut échouer sans toucher au disque est vérifié d'abord :
    # mieux vaut refuser en deux secondes que vingt minutes plus tard.
    try:
        spec = resolve_encoder(config.audio_format)
    except (MissingToolError, RipError) as exc:
        _error(str(exc))
        return 2

    if not args.dry_run:
        try:
            config.validate()
        except ConfigError as exc:
            _error(str(exc))
            return 2

    api = JewelBoxApi(config.server_url)
    server_ok = api.ping()

    if not server_ok:
        _warn(f'Serveur injoignable sur {config.server_url}.')
        _info('Sans lui, impossible de vérifier la collection ni de lancer '
              'le scan : les fichiers extraits resteraient invisibles dans '
              "l'application.")
        if not _confirm('Extraire quand même ?', default=False,
                        assume_yes=args.yes):
            return 4

    # -- 1. identification -------------------------------------------------

    try:
        disc_id, track_count = read_disc_id(config.device)
    except DiscReadError as exc:
        _error(str(exc))
        return 3

    _info(f'Disque détecté : {track_count} pistes (Disc ID {disc_id})')

    try:
        candidates = lookup_candidates(disc_id)
    except IdentificationError as exc:
        _warn(str(exc))
        candidates = []

    # L'API n'est transmise que si le serveur répond : inutile d'attendre un
    # nouveau délai d'expiration pour lui réclamer ses identifiants Discogs.
    disc = _choose_disc(candidates, disc_id, track_count, assume_yes=args.yes,
                        config=config, api=api if server_ok else None)
    if disc is None:
        _warn('Abandon : disque non identifié.')
        return 3

    _print_disc(disc, config)

    # -- 2. collection -----------------------------------------------------

    album_dir, planned = plan_paths(disc, config)

    if args.dry_run:
        _print_plan(album_dir, planned, disc)
        if server_ok:
            _check_collection(api, disc, dry_run=True, assume_yes=args.yes)
        return 0

    if server_ok and not _check_collection(api, disc, dry_run=False,
                                           assume_yes=args.yes):
        return 4

    # -- 3. extraction -----------------------------------------------------

    if not _confirm(f'Extraire {len(disc.tracks)} pistes en '
                    f'{spec.name.upper()} ?', default=True, assume_yes=args.yes):
        return 0

    started = time.monotonic()
    try:
        # Hors terminal (service, redirection), une barre rafraîchie deux fois
        # par seconde noierait le journal : seules les étapes y sont écrites.
        if sys.stdout.isatty():
            display = _RipDisplay()
            result = rip_disc(disc, config, spec, progress=display.step,
                              track_progress=display.update)
        else:
            result = rip_disc(disc, config, spec, progress=_progress)
    except PipelineError as exc:
        _error(str(exc))
        return 5

    elapsed = int(time.monotonic() - started)
    print()
    _info(f'{len(result.files)} pistes extraites en {elapsed // 60} min '
          f'{elapsed % 60} s')
    _info(f'Dossier : {result.album_dir}')

    for number, message in result.failed:
        _warn(f'Piste {number} échouée : {message}')
    for number in result.degraded:
        _warn(f'Piste {number} : lecture vérifiée trop lente, relue sans '
              'correction d\'erreurs. Elle peut contenir de courtes coupures : '
              'à réécouter.')
    if result.replaygain is not None:
        if result.replaygain.applied:
            _info('Volume normalisé : tags ReplayGain écrits (piste et album)')
        else:
            _warn(f'ReplayGain : {result.replaygain.message}')

    if not args.no_eject:
        eject(config.device)

    # -- 4. appariement ----------------------------------------------------

    if server_ok and not args.no_scan:
        _run_scan(api)

    return 0 if result.ok else 6


# -- identification ------------------------------------------------------


def _choose_disc(candidates: list[ReleaseCandidate], disc_id: str,
                 track_count: int, *, assume_yes: bool,
                 config: Config | None = None,
                 api: JewelBoxApi | None = None) -> DiscInfo | None:
    """Choisit l'édition, avec repli sur Discogs puis sur la saisie manuelle."""
    if not candidates:
        _warn('Disque inconnu de MusicBrainz.')
        if assume_yes:
            return None

        disc = _discogs_fallback(track_count, config, api)
        if disc is not None:
            return disc

        if not _confirm('Saisir les informations à la main ?', default=True,
                        assume_yes=False):
            return None
        return _manual_disc(track_count)

    if len(candidates) == 1:
        chosen = candidates[0]
        _info(f'Identifié : {chosen.summary()}')
    else:
        _info(f'{len(candidates)} éditions correspondent à ce disque :')
        for index, candidate in enumerate(candidates, start=1):
            print(f'  {index:2d}. {candidate.summary()}')

        if assume_yes:
            chosen = candidates[0]
            _info(f'Sélection automatique : {chosen.summary()}')
        else:
            choice = _ask_index('Quelle édition ?', len(candidates))
            if choice is None:
                return None
            chosen = candidates[choice]

    disc = build_disc_info(chosen, disc_id=disc_id)

    # Un écart de nombre de pistes trahit un mauvais support choisi dans un
    # coffret, ou des métadonnées incomplètes.
    if disc.tracks and len(disc.tracks) != track_count:
        _warn(f'Le disque contient {track_count} pistes, mais cette édition '
              f'en déclare {len(disc.tracks)}.')

    return disc


def _prompt(label: str) -> str:
    """Lit une réponse. Ctrl+D et fin de flux valent une entrée vide.

    Sans ça, un Ctrl+D pendant la saisie ferait remonter une EOFError jusqu'au
    sommet et afficherait une trace Python. L'appelant traite la chaîne vide
    comme il l'entend : annulation sur un champ requis, valeur par défaut
    ailleurs — c'est exactement ce que fait déjà la touche Entrée.
    """
    try:
        return input(label).strip()
    except EOFError:
        # Le curseur reste sur la ligne du prompt abandonné.
        print()
        return ''


def _discogs_credentials(config: Config | None,
                         api: JewelBoxApi | None) -> DiscogsCredentials | None:
    """Trouve de quoi s'authentifier, du serveur d'abord puis du local.

    Le serveur JewelBox enregistre déjà un couple clé/secret Discogs dans ses
    réglages : le reprendre évite d'avoir à créer un second jeu d'identifiants
    pour le ripper. S'il est absent ou mal rempli, on retombe sur le jeton
    personnel de la configuration locale.
    """
    if api is not None:
        pair = api.discogs_credentials()
        if pair:
            key, secret = pair
            return DiscogsCredentials(key=key, secret=secret,
                                      source='réglages du serveur')

    token = config.discogs_token if config else None
    if token:
        return DiscogsCredentials(token=token,
                                  source='configuration du ripper')

    return None


def _discogs_fallback(track_count: int, config: Config | None,
                      api: JewelBoxApi | None = None) -> DiscInfo | None:
    """Cherche le disque sur Discogs. Renvoie None pour passer au manuel.

    Discogs n'indexe pas les Disc ID : impossible de retrouver le disque
    automatiquement, la recherche part donc de termes saisis à la main. On y
    gagne malgré tout la tracklist complète plutôt que douze titres à retaper.
    """
    credentials = _discogs_credentials(config, api)
    if credentials is None:
        _info('Recherche Discogs indisponible : aucun identifiant.')
        _info('  Pour l\'activer, crée un jeton sur '
              'https://www.discogs.com/settings/developers')
        _info('  puis renseigne « discogs_token » dans ta configuration '
              '(ou la variable DISCOGS_TOKEN).')
        return None

    if not _confirm('Chercher sur Discogs ?', default=True, assume_yes=False):
        return None

    try:
        client = DiscogsClient(credentials)
    except DiscogsError as exc:
        _warn(str(exc))
        return None

    print('\nRecherche Discogs (Entrée pour passer à la saisie manuelle) :')
    artist = _prompt('  Artiste : ')
    album = _prompt('  Album   : ')

    if not artist and not album:
        return None

    try:
        releases = client.search(artist, album, track_count=track_count)
    except DiscogsError as exc:
        _warn(str(exc))
        return None

    if not releases:
        _warn('Aucun résultat sur Discogs.')
        return None

    chosen = _choose_release(releases, track_count)
    if chosen is None:
        return None

    try:
        detail = client.fetch_tracklist(chosen.release_id)
    except DiscogsError as exc:
        _warn(f'Tracklist illisible : {exc}')
        return None

    disc = build_discogs_disc_info(detail, fallback_track_count=track_count)

    if not disc.tracks:
        _warn('Cette release ne déclare aucune piste sur Discogs.')
        return None

    if len(disc.tracks) != track_count:
        _warn(f'Le disque contient {track_count} pistes, mais cette édition '
              f'en déclare {len(disc.tracks)}.')
        if not _confirm('Utiliser quand même ces métadonnées ?', default=False,
                        assume_yes=False):
            return None

    _info(f'Retenu depuis Discogs : {disc.artist} — {disc.album}')
    return disc


def _choose_release(releases: list[DiscogsRelease],
                    track_count: int) -> DiscogsRelease | None:
    """Affiche les releases Discogs et fait choisir."""
    _info(f'{len(releases)} release(s) trouvée(s) sur Discogs :')
    for index, release in enumerate(releases, start=1):
        # Le repère signale les éditions dont le nombre de pistes correspond
        # au disque inséré : ce sont les candidates les plus probables.
        marker = ' ←' if release.track_count == track_count else ''
        print(f'  {index:2d}. {release.summary()}{marker}')

    choice = _ask_index('Quelle édition ?', len(releases))
    if choice is None:
        return None
    return releases[choice]


def _manual_disc(track_count: int) -> DiscInfo | None:
    """Saisie manuelle, pour les pressages absents de MusicBrainz."""
    print('\nSaisie manuelle (Entrée pour annuler) :')
    artist = _prompt('  Artiste : ')
    if not artist:
        return None
    album = _prompt('  Album   : ')
    if not album:
        return None

    year_raw = _prompt('  Année (facultatif) : ')
    year = int(year_raw) if year_raw.isdigit() else None
    genre = _prompt('  Genre (facultatif) : ') or None

    print(f'\n  Titres des {track_count} pistes '
          '(Entrée pour « Piste N ») :')
    tracks: list[TrackInfo] = []
    for number in range(1, track_count + 1):
        # Artiste et album sont déjà saisis : un Ctrl+D ici valide les titres
        # restants en « Piste N » plutôt que de tout jeter.
        title = _prompt(f'    {number:2d}. ') or f'Piste {number}'
        tracks.append(TrackInfo(number=number, title=title))

    return DiscInfo(artist=artist, album=album, tracks=tracks,
                    year=year, genre=genre)


# -- collection ----------------------------------------------------------


def _check_collection(api: JewelBoxApi, disc: DiscInfo, *,
                      dry_run: bool, assume_yes: bool) -> bool:
    """Vérifie la présence de l'album et propose de l'ajouter si absent.

    Renvoie False si l'utilisateur souhaite interrompre.
    """
    album_artist = disc.album_artist()

    try:
        existing = api.find_duplicate(disc.album, album_artist)
    except ApiError as exc:
        _warn(f'Vérification de la collection impossible : {exc}')
        return True

    if existing:
        title = existing.get('title', disc.album)
        _info(f'Déjà dans la collection : « {title} » (#{existing.get("id")})')

        if existing.get('is_wanted'):
            _info('Cet album est dans ta liste de souhaits.')
            if dry_run:
                _info('  → serait marqué comme possédé')
            elif _confirm('Le marquer comme possédé ?', default=True,
                          assume_yes=assume_yes):
                try:
                    api.clear_wanted(int(existing['id']))
                    _info('Album marqué comme possédé.')
                except (ApiError, KeyError, ValueError) as exc:
                    _warn(f'Mise à jour impossible : {exc}')

        if disc.disc_total > 1:
            _add_missing_disc(api, existing, disc, dry_run=dry_run,
                              assume_yes=assume_yes)
        return True

    # Absent : la comparaison serveur est stricte alors que le scanner
    # normalise bien plus largement, donc « absent » n'est pas une certitude.
    _warn(f'Absent de la collection : « {disc.album} » — {album_artist}')
    _info('Sans album correspondant, les fichiers extraits resteront '
          'non appariés après le scan.')

    if dry_run:
        _info('  → serait proposé à la création via POST /api/albums')
        return True

    if not _confirm('Créer cet album dans la collection ?', default=True,
                    assume_yes=assume_yes):
        _warn('Album non créé — les fichiers ne seront pas appariés.')
        return True

    payload = AlbumPayload(
        title=disc.album,
        artist_name=album_artist,
        year=disc.year,
        genre=disc.genre,
        label_name=disc.label,
        total_duration=disc.total_duration,
        ean=disc.barcode,
        cover_url=disc.cover_url,
        tracks=[
            TrackPayload(position=t.number, title=t.title,
                         duration=t.duration_label, disc=disc.disc_number)
            for t in disc.tracks
        ],
    )

    try:
        created = api.create_album(payload)
        _info(f'Album créé (#{created.get("id")}) avec '
              f'{len(disc.tracks)} pistes.')
        if disc.cover_url:
            _info('Pochette récupérée par le serveur depuis Cover Art Archive.')
    except AlbumConflict as exc:
        # Le serveur a détecté un doublon que la route duplicate n'a pas vu
        # (EAN identique, ou titre normalisé différemment).
        _warn(f'{exc}')
        _info('Les fichiers s\'apparieront probablement à l\'album existant.')
    except ApiError as exc:
        _error(f'Création impossible : {exc}')
        return _confirm('Extraire quand même ?', default=False,
                        assume_yes=assume_yes)

    return True


def _add_missing_disc(api: JewelBoxApi, existing: dict[str, Any],
                      disc: DiscInfo, *, dry_run: bool,
                      assume_yes: bool) -> None:
    """Ajoute les pistes du CD inséré à un album qui ne les a pas encore.

    Sur un coffret, l'album est créé avec le premier CD extrait : les
    suivants doivent y ajouter leurs pistes, faute de quoi leurs fichiers
    resteraient non appariés.
    """
    try:
        album_id = int(existing['id'])
        album = api.get_album(album_id)
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        _warn(f'Lecture des pistes de l\'album impossible : {exc}')
        return

    tracks = album.get('tracks') or []
    if any(_disc_of(t) == disc.disc_number for t in tracks):
        return

    # Un coffret saisi avant la gestion des CD a toutes ses pistes sur le
    # CD 1 : y ajouter celles-ci les mettrait en double.
    known = {str(t.get('title') or '').casefold() for t in tracks}
    already = sum(1 for t in disc.tracks if t.title.casefold() in known)
    if disc.tracks and already * 2 >= len(disc.tracks):
        _warn(f'Les pistes du CD {disc.disc_number} semblent déjà saisies '
              'sur un autre CD de cet album : range-les dans le bon CD '
              'depuis la fiche de l\'album.')
        return

    _info(f'Le CD {disc.disc_number} n\'a pas encore de pistes dans cet album.')
    if dry_run:
        _info(f'  → ses {len(disc.tracks)} pistes seraient ajoutées')
        return
    if not _confirm(f'Ajouter les {len(disc.tracks)} pistes du CD '
                    f'{disc.disc_number} à cet album ?', default=True,
                    assume_yes=assume_yes):
        _warn('Pistes non ajoutées — les fichiers ne seront pas appariés.')
        return

    # PATCH remplace la liste : les pistes existantes sont renvoyées telles
    # quelles (le serveur les reconnaît à leur CD et leur numéro).
    payload = [
        {key: t[key] for key in ('disc', 'position', 'title', 'duration')
         if t.get(key) is not None}
        for t in tracks
    ]
    payload += [
        TrackPayload(position=t.number, title=t.title,
                     duration=t.duration_label,
                     disc=disc.disc_number).to_json()
        for t in disc.tracks
    ]
    try:
        api.update_album(album_id, {'tracks': payload})
        _info(f'{len(disc.tracks)} pistes ajoutées (CD {disc.disc_number}).')
    except ApiError as exc:
        _warn(f'Ajout des pistes impossible : {exc}')


def _disc_of(track: dict[str, Any]) -> int:
    try:
        return int(track.get('disc') or 1)
    except (TypeError, ValueError):
        return 1


def _run_scan(api: JewelBoxApi) -> None:
    """Déclenche l'appariement et rend compte du résultat."""
    _info('Lancement du scan de la bibliothèque…')
    try:
        api.start_scan()
    except ApiError as exc:
        _warn(f'Scan impossible : {exc}')
        _info('Lance-le à la main depuis les réglages du serveur.')
        return

    # Le scan est asynchrone côté serveur : on interroge son statut.
    for _ in range(120):
        time.sleep(1.0)
        try:
            status = api.scan_status()
        except ApiError:
            break

        if not status.get('running'):
            result = status.get('result') or {}
            matched = result.get('matched_tracks', 0)
            albums = result.get('matched_albums', 0)
            unmatched = result.get('unmatched_count', 0)

            _info(f'Scan terminé : {matched} pistes appariées '
                  f'sur {albums} albums.')
            if unmatched:
                _warn(f'{unmatched} fichiers non appariés dans la bibliothèque.')
            return

    _warn('Le scan est toujours en cours — consulte les réglages du serveur.')


# -- affichage -----------------------------------------------------------


def _print_disc(disc: DiscInfo, config: Config) -> None:
    print()
    print(f'  Artiste : {disc.album_artist()}')
    print(f'  Album   : {disc.album}')
    if disc.year:
        print(f'  Année   : {disc.year}')
    if disc.label:
        print(f'  Label   : {disc.label}')
    if disc.disc_total > 1:
        print(f'  Disque  : {disc.disc_number}/{disc.disc_total}')
    print(f'  Pistes  : {len(disc.tracks)}'
          + (f' · {disc.total_duration}' if disc.total_duration else ''))
    print(f'  Format  : {config.audio_format.upper()}')
    print()


def _print_plan(album_dir: Path, planned: list[tuple[int, Path]],
                disc: DiscInfo) -> None:
    print('Fichiers qui seraient créés :')
    print(f'  {album_dir}{"/" if planned else ""}')
    by_number = {t.number: t for t in disc.tracks}
    for number, path in planned:
        track = by_number.get(number)
        duration = f'  ({track.duration_label})' if track and track.duration_label else ''
        print(f'    {path.name}{duration}')
    print()


def _progress(index: int, total: int, label: str) -> None:
    # \r pour une ligne unique qui se réécrit, sans noyer le terminal.
    print(f'\r  [{index:2d}/{total}] {label[:60]:<60}', end='', flush=True)


def format_progress_line(step: str, fraction: float | None, phase: str,
                         elapsed_seconds: float, width: int) -> str:
    """Compose la ligne de progression, sans jamais dépasser `width`.

    Une ligne plus large que le terminal passerait à la ligne, et le \\r ne
    réécrirait plus que sa dernière partie.
    """
    minutes, seconds = divmod(int(elapsed_seconds), 60)
    if fraction is None:
        # Durée inconnue : pas de pourcentage à afficher sans mentir.
        gauge = ''
    else:
        filled = round(fraction * BAR_WIDTH)
        gauge = (f'{"█" * filled}{"░" * (BAR_WIDTH - filled)} '
                 f'{int(fraction * 100):3d} %  ')
    suffix = f'  {gauge}{phase}  {minutes}:{seconds:02d}'

    room = max(0, width - 1 - len(suffix))
    return (step[:room].ljust(room) + suffix)[:max(0, width - 1)]


class _RipDisplay:
    """Affiche l'étape en cours et l'avancement de la piste sur une seule ligne."""

    def __init__(self) -> None:
        self._step = ''
        self._phase: str | None = None
        self._started = 0.0

    def step(self, index: int, total: int, label: str) -> None:
        self._step = f'  [{index:2d}/{total}] {label}'
        self._phase = None
        self._write(self._step)

    def update(self, fraction: float | None, phase: str) -> None:
        # Le temps repart à chaque étape : c'est lui qui montre une lecture
        # qui stagne, et la relecture rapide doit donc recommencer à zéro.
        if phase != self._phase:
            self._phase = phase
            self._started = time.monotonic()
        self._write(format_progress_line(
            self._step, fraction, phase, time.monotonic() - self._started,
            self._width()))

    def _write(self, line: str) -> None:
        print(f'\r{line[:self._width() - 1]:<{self._width() - 1}}',
              end='', flush=True)

    @staticmethod
    def _width() -> int:
        return shutil.get_terminal_size().columns


def _confirm(question: str, *, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return default
    if not sys.stdin.isatty():
        return default

    suffix = '[O/n]' if default else '[o/N]'
    try:
        answer = input(f'{question} {suffix} ').strip().lower()
    except EOFError:
        return default

    if not answer:
        return default
    return answer in ('o', 'oui', 'y', 'yes')


def _ask_index(question: str, count: int) -> int | None:
    """Demande un numéro dans 1..count. Renvoie l'index 0-based, ou None."""
    if not sys.stdin.isatty():
        return 0

    for _ in range(3):
        try:
            answer = input(f'{question} [1-{count}, Entrée pour annuler] ').strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        _warn(f'Réponse invalide : saisis un nombre entre 1 et {count}.')
    return None


def _info(message: str) -> None:
    print(f'  {message}')


def _warn(message: str) -> None:
    print(f'  ! {message}', file=sys.stderr)


def _error(message: str) -> None:
    print(f'  ✗ {message}', file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
