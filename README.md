# JewelBox Music Ripper

Extraction de CD audio vers une bibliothèque
[JewelBox Music Library](https://github.com/William-De71/jewelbox-music-library).

Insère un CD, lance `jewelbox-rip` : le disque est identifié via MusicBrainz,
ajouté à ta collection s'il en est absent, extrait en FLAC (ou Opus/MP3), tagué
et déposé dans la bibliothèque du serveur, qui l'apparie ensuite tout seul.

## Comment ça s'articule avec le serveur

Point important : **le serveur ne crée pas d'albums à partir des fichiers.** Son
scanner part des albums déjà présents en base et cherche à leur associer des
fichiers ; tout fichier sans album correspondant reste « non apparié » et
n'apparaît nulle part dans l'application.

C'est pour cette raison que le ripper interroge l'API avant d'extraire : si
l'album n'est pas dans ta collection, il propose de l'y créer — sinon
l'extraction ne servirait à rien.

```
CD → Disc ID → MusicBrainz → confirmation
  ↓
GET /api/albums/duplicate
  ├── présent → propose de lever « souhaité » le cas échéant
  └── absent  → propose POST /api/albums (avec pistes et pochette)
  ↓
extraction → encodage → tags        (dans un dossier temporaire)
  ↓
publication atomique : Artiste/Album/01 - Titre.flac
  ↓
POST /api/player/scan → appariement → bilan
```

Rien n'est publié tant que le disque n'est pas entièrement extrait : une
interruption ne laisse aucun album à moitié écrit dans la bibliothèque.

## Installation

Dépendances système (Fedora) :

```bash
sudo dnf install cdparanoia flac python3-mutagen python3-discid \
                 python3-musicbrainzngs
# formats optionnels
sudo dnf install opus-tools lame
```

Puis, une fois pour toutes :

```bash
make install    # installe la commande « jewelbox-rip »
make config     # crée ta configuration depuis l'exemple
```

`make install` pose `jewelbox-rip` dans `~/.local/bin` en mode éditable : les
modifications du code sont prises en compte sans réinstaller.

## Configuration

`make config` copie `config.example.toml` vers
`~/.config/jewelbox-music-ripper/config.toml` (sans jamais écraser un fichier
existant). Une seule ligne est à renseigner :

```toml
library_path = "/mnt/musique"
```

Ce doit être le dossier scanné par le serveur. Il n'est pas devinable : le
serveur range ce chemin en base et ne l'expose par aucune route.

## Utilisation

Une fois configuré, tout tient en une commande :

```bash
jewelbox-rip
```

Insère un CD, lance ça, réponds aux questions. C'est tout.

```bash
jewelbox-rip --dry-run          # montre ce qui serait fait, n'écrit rien
jewelbox-rip -f opus -q 192     # autre format
jewelbox-rip --fast             # sans correction d'erreurs (disque impeccable)
jewelbox-rip -y                 # non interactif, réponses par défaut
```

Options utiles : `--library`, `--server`, `--device`, `--no-scan`, `--no-eject`.

Commence par `--dry-run` sur un disque : il affiche l'arborescence qui serait
produite et l'état de l'album dans la collection, sans rien modifier.

### Sans installer

`make dry-run` et `make rip` lancent l'outil depuis les sources, sans passer par
`make install`.

## Choix techniques

**Correction d'erreurs par défaut.** `cdparanoia` relit les secteurs douteux
plutôt que d'accepter des clics. C'est plus lent, et c'est l'intérêt pour de
l'archivage — `--fast` désactive ce comportement.

**Nommage aligné sur le scanner.** Les fichiers suivent
`Artiste/Album/01 - Titre.flac`, exactement ce que le repli `parsePathFallback()`
du serveur sait relire quand les tags manquent. Les tags restent la voie
principale d'appariement ; l'arborescence est la ceinture de sécurité.

**Accents conservés.** La normalisation du serveur retire les diacritiques avant
comparaison : « Björk » s'apparie sans souci, donc rien ne justifie de dégrader
les noms de fichiers. Seuls les caractères réellement problématiques (`/`, `:`,
contrôles) sont remplacés.

**Pochette déléguée au serveur.** Le ripper transmet l'URL Cover Art Archive de
la release ; `POST /api/albums` reconnaît ce domaine et télécharge l'image
lui-même. Rien à stocker côté ripper.

**Compilations.** Détectées par l'artiste (`Various Artists`) ou l'hétérogénéité
des pistes, elles sont classées sous « Artistes divers » avec l'artiste réel
conservé au niveau de chaque piste.

**Coffrets.** Le support extrait est choisi par son Disc ID, pas par sa position :
sur un coffret, c'est bien le disque inséré qui est lu. Les disques multiples
vont dans des sous-dossiers `CD1`, `CD2`… (un seul niveau, ce que le serveur sait
parcourir).

## Tests

```bash
make test          # ou : python3 -m pytest
```

## Publier une version

```bash
./release.sh patch      # 0.1.0 → 0.1.1
./release.sh minor      # 0.1.0 → 0.2.0
./release.sh 1.0.0      # version explicite
```

Le script rejoue les tests, incrémente `__version__` dans
`src/jewelbox_ripper/__init__.py` — la seule source de vérité, `pyproject.toml`
la lit dynamiquement —, vérifie que le paquet se construit, puis crée le commit
et le tag `vX.Y.Z` et les pousse.

Le tag déclenche `.github/workflows/release.yml`, qui rejoue la gate, contrôle
que le tag correspond bien à `__version__`, construit le wheel et le sdist, et
publie la GitHub Release avec les deux artefacts.
