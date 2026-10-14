# Camera-dvr
Système DVR de détection de mouvement pour caméras IP/RTSP ou webcam locale,avec reconnexion automatique, enregistrement segmenté par date, rétentionautomatique et mode headless pour serveur.

Auteur : Eddy Okito 
E-mail : eddyokito@gmail.com 
Tél.   : +243895755065

Kinshasa / RD Congo 


# camera-dvr

Système DVR de détection de mouvement pour caméras IP/RTSP ou webcam locale,
avec reconnexion automatique, enregistrement segmenté par date, rétention
automatique et mode headless pour serveur.

## Fonctionnalités

- Capture vidéo threadée avec reconnexion automatique en cas de coupure RTSP
- Détection de mouvement avec sensibilité adaptative selon l'heure
- Enregistrement vidéo segmenté par date (`enregistrements/AAAA-MM-JJ/`)
- Purge automatique des enregistrements selon une durée de rétention
- Mode headless (sans affichage) pour déploiement serveur
- Journalisation fichier avec rotation (`logs/dvr.log`)
- Arrêt propre (Ctrl+C / SIGTERM)
- Vérification de l'espace disque avant écriture

## Prérequis

- Python 3.9 ou supérieur
- Une caméra locale (webcam) ou un flux RTSP accessible sur le réseau

## Installation

### Option A — Script automatique (recommandé)

```bash
chmod +x install.sh
./install.sh
```

Crée un environnement virtuel `.venv/` et installe le paquet dedans.

### Option B — Installation manuelle avec pip

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install .
```

### Option C — Sans installation (dépendances seules)

```bash
pip install -r requirements.txt
python -m camera_dvr.dvr --source 0 --zone "Ma_Camera"
```

## Utilisation

Une fois installé (options A ou B), la commande `camera-dvr` est disponible :

```bash
# Caméra locale (webcam), avec affichage
camera-dvr --source 0 --zone "Webcam"

# Flux RTSP, sans affichage (serveur), avec rétention de 14 jours
camera-dvr --source "rtsp://admin:motdepasse@192.168.1.126" \
           --zone "Entree_Stade" --headless --retention-jours 14
```

### Options disponibles

| Option | Description | Défaut |
|---|---|---|
| `--source` | Index caméra locale (ex: `0`) ou URL RTSP | *(requis)* |
| `--zone` | Nom de la caméra / zone | `Camera_1` |
| `--sortie` | Dossier de sortie des vidéos | `enregistrements` |
| `--duree-prolongation` | Secondes sans mouvement avant d'arrêter l'enregistrement | `5.0` |
| `--fps` | FPS du fichier de sortie | `20.0` |
| `--codec` | Codec FOURCC (`XVID`, `MJPG`, `mp4v`...) | `XVID` |
| `--headless` | Désactive l'affichage | désactivé |
| `--retention-jours` | Jours avant purge automatique (`0` = désactivé) | `30` |
| `--espace-min-go` | Seuil d'espace disque minimal avant suspension | `1.0` |

## Exécution en service (Linux, démarrage automatique)

1. Copiez le paquet installé sur le serveur, par exemple dans `/opt/camera-dvr`.
2. Adaptez `systemd/camera-dvr.service` (chemin, source RTSP, utilisateur système).
3. Installez le service :

```bash
sudo cp systemd/camera-dvr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-dvr.service
sudo systemctl status camera-dvr.service
journalctl -u camera-dvr.service -f    # suivre les logs en direct
```

## Structure du projet

```
camera_dvr_package/
├── camera_dvr/
│   ├── __init__.py
│   ├── dvr.py          # logique principale (capture, détection, enregistrement)
│   └── cli.py           # point d'entrée de la commande camera-dvr
├── systemd/
│   └── camera-dvr.service
├── pyproject.toml
├── requirements.txt
├── install.sh
└── README.md
```

## Désinstallation

```bash
pip uninstall camera-dvr
rm -rf .venv
```
