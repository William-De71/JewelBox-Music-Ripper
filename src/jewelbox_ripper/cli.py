"""Interface en ligne de commande du ripper.

Déroulé d'une exécution :

  1. lire le Disc ID du CD inséré ;
  2. interroger MusicBrainz, faire choisir l'édition (ou saisir à la main) ;
  3. vérifier la présence de l'album dans la collection JewelBox ;
     - absent  → proposer de le créer via l'API ;
     - présent et « souhaité » → proposer de le marquer possédé ;
  4. extraire, encoder, taguer, publier ;
  5. déclencher le scan du serveur et rendre compte de l'appariement.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .api import AlbumConflict, AlbumPayload, ApiError, JewelBoxApi, TrackPayload
from .config import Config, ConfigError, build_config, default_config_path, load_config
from .disc import DiscInfo, TrackInfo, format_duration
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = _resolve_config(args)
    except ConfigError as exc:
        _error(str(exc))
        return 2

    try:
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

    disc = _choose_disc(candidates, disc_id, track_count, assume_yes=args.yes)
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

    if not args.no_eject:
        eject(config.device)

    # -- 4. appariement ----------------------------------------------------

    if server_ok and not args.no_scan:
        _run_scan(api)

    return 0 if result.ok else 6


# -- identification ------------------------------------------------------


def _choose_disc(candidates: list[ReleaseCandidate], disc_id: str,
                 track_count: int, *, assume_yes: bool) -> DiscInfo | None:
    """Choisit l'édition, ou bascule sur une saisie manuelle."""
    if not candidates:
        _warn('Disque inconnu de MusicBrainz.')
        if assume_yes:
            return None
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


def _manual_disc(track_count: int) -> DiscInfo | None:
    """Saisie manuelle, pour les pressages absents de MusicBrainz."""
    print('\nSaisie manuelle (Entrée pour annuler) :')
    artist = input('  Artiste : ').strip()
    if not artist:
        return None
    album = input('  Album   : ').strip()
    if not album:
        return None

    year_raw = input('  Année (facultatif) : ').strip()
    year = int(year_raw) if year_raw.isdigit() else None
    genre = input('  Genre (facultatif) : ').strip() or None

    print(f'\n  Titres des {track_count} pistes '
          '(Entrée pour « Piste N ») :')
    tracks: list[TrackInfo] = []
    for number in range(1, track_count + 1):
        title = input(f'    {number:2d}. ').strip() or f'Piste {number}'
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
                         duration=t.duration_label)
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
