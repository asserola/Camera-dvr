#!/usr/bin/env python3
"""
Système DVR de détection de mouvement pour caméras IP/RTSP.

Auteur : Eddy Okito 
E-mail : eddyokito@gmail.com 
Tél.   : +243895755065



Fonctionnalités :
    - Capture vidéo threadée avec reconnexion automatique en cas de coupure RTSP
    - Détection de mouvement avec sensibilité adaptative selon l'heure
    - Enregistrement vidéo segmenté par date, avec purge automatique (rétention)
    - Mode headless (sans affichage) pour déploiement serveur / service
    - Journalisation (logging) dans un fichier + console, avec rotation
    - Configuration via arguments CLI (plus besoin d'éditer le code)
    - Arrêt propre (Ctrl+C / SIGTERM) sans corrompre le fichier vidéo en cours
    - Vérification de l'espace disque disponible avant d'écrire
    - Support multi-caméras (plusieurs instances en parallèle)

Dépendances :
    pip install opencv-python

Exemple (après installation du paquet) :
    camera-dvr --source "rtsp://admin:admin@192.168.1.126" --zone "Entree_Stade"
    camera-dvr --source 0 --zone "Webcam" --headless

    # Ou sans installation, depuis la racine du paquet :
    python -m camera_dvr.dvr --source 0 --zone "Webcam"
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class DVRConfig:
    """Paramètres de configuration du système DVR."""

    source: str                        # Index caméra (0) ou URL RTSP
    nom_zone: str = "Camera_1"
    dossier_sortie: str = "enregistrements"
    duree_prolongation: float = 5.0    # secondes sans mouvement avant d'arrêter l'enreg.
    fps_sortie: float = 20.0
    codec: str = "XVID"
    headless: bool = False
    retention_jours: int = 30          # 0 = pas de purge automatique
    espace_disque_min_go: float = 1.0  # seuil d'arrêt de sécurité
    reconnect_delai_sec: float = 3.0
    reconnect_max_essais: int = 0      # 0 = illimité


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def configurer_logging(dossier_logs: str = "logs") -> logging.Logger:
    """Configure un logger avec rotation de fichiers + sortie console."""
    Path(dossier_logs).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dvr")
    logger.setLevel(logging.INFO)

    if logger.handlers:  # évite les doublons si appelé plusieurs fois
        return logger

    formatteur = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fichier_handler = logging.handlers.RotatingFileHandler(
        os.path.join(dossier_logs, "dvr.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fichier_handler.setFormatter(formatteur)
    logger.addHandler(fichier_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatteur)
    logger.addHandler(console_handler)

    return logger


# --------------------------------------------------------------------------- #
# Flux caméra threadé avec reconnexion automatique
# --------------------------------------------------------------------------- #

class CameraStream:
    """Capture vidéo en tâche de fond, avec reconnexion automatique si le flux tombe."""

    def __init__(self, config: DVRConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.source = config.source

        self._lock = threading.Lock()
        self._stream: Optional[cv2.VideoCapture] = None
        self._ret = False
        self._frame = None
        self._en_cours = True
        self._connecte = False
        self._essais_reconnexion = 0

        self._ouvrir_flux()

        self._thread = threading.Thread(
            target=self._boucle_capture, name="CameraStream", daemon=True
        )
        self._thread.start()

    def _ouvrir_flux(self) -> bool:
        try:
            self._stream = cv2.VideoCapture(self.source)
        except Exception as exc:  # pragma: no cover - dépend du backend OpenCV
            self.logger.error("Exception à l'ouverture du flux : %s", exc)
            self._connecte = False
            return False

        if self._stream is not None and self._stream.isOpened():
            self._connecte = True
            self._essais_reconnexion = 0
            self.logger.info("Flux caméra ouvert : %s", self.source)
            return True

        self._connecte = False
        return False

    def _boucle_capture(self) -> None:
        while self._en_cours:
            if not self._connecte or self._stream is None or not self._stream.isOpened():
                self._tenter_reconnexion()
                continue

            try:
                ret, frame = self._stream.read()
            except Exception as exc:  # pragma: no cover
                self.logger.warning("Erreur de lecture du flux : %s", exc)
                ret, frame = False, None

            if not ret:
                self.logger.warning("Flux perdu (lecture échouée). Tentative de reconnexion...")
                self._connecte = False
                continue

            with self._lock:
                self._ret, self._frame = ret, frame

            time.sleep(0.01)

    def _tenter_reconnexion(self) -> None:
        if self._stream is not None:
            self._stream.release()

        if (
            self.config.reconnect_max_essais
            and self._essais_reconnexion >= self.config.reconnect_max_essais
        ):
            self.logger.error(
                "Nombre maximal de tentatives de reconnexion atteint (%d). Arrêt du flux.",
                self.config.reconnect_max_essais,
            )
            self._en_cours = False
            return

        self._essais_reconnexion += 1
        self.logger.info(
            "Reconnexion au flux dans %.1fs (essai n°%d)...",
            self.config.reconnect_delai_sec,
            self._essais_reconnexion,
        )
        time.sleep(self.config.reconnect_delai_sec)
        self._ouvrir_flux()

    def lire_frame(self):
        with self._lock:
            return self._ret, self._frame

    def est_actif(self) -> bool:
        return self._en_cours

    def arreter(self) -> None:
        self._en_cours = False
        self._thread.join(timeout=2.0)
        if self._stream is not None:
            self._stream.release()


# --------------------------------------------------------------------------- #
# Enregistreur avec détection de mouvement
# --------------------------------------------------------------------------- #

class StadeEnregistreurMouvement:
    """Détecte le mouvement sur un flux vidéo et enregistre des séquences classées par date."""

    def __init__(self, config: DVRConfig):
        self.config = config
        self.logger = configurer_logging()
        self.cam_stream: Optional[CameraStream] = None

        self.dossier_dvr_base = Path(config.dossier_sortie)
        self.dossier_dvr_base.mkdir(parents=True, exist_ok=True)

        self.en_train_d_enregistrer = False
        self.video_writer: Optional[cv2.VideoWriter] = None
        self.fichier_en_cours: Optional[Path] = None
        self.dernier_mouvement_temps = 0.0

        self._arret_demande = threading.Event()
        signal.signal(signal.SIGINT, self._gestionnaire_arret)
        signal.signal(signal.SIGTERM, self._gestionnaire_arret)

    def _gestionnaire_arret(self, signum, frame) -> None:  # noqa: ANN001
        self.logger.info("Signal d'arrêt reçu (%s). Fermeture propre en cours...", signum)
        self._arret_demande.set()

    # ------------------------------------------------------------------ #
    # Utilitaires
    # ------------------------------------------------------------------ #

    def ajuster_sensibilite(self) -> int:
        """Ajuste dynamiquement la surface minimale de détection selon l'heure."""
        heure = datetime.now().hour
        if 6 <= heure < 18:
            return 1500   # Journée : moins sensible
        if 18 <= heure < 22:
            return 1000   # Soirée : sensibilité moyenne
        return 500        # Nuit : haute sensibilité

    def _espace_disque_suffisant(self) -> bool:
        try:
            _, _, libre = shutil.disk_usage(self.dossier_dvr_base)
            libre_go = libre / (1024 ** 3)
        except OSError as exc:
            self.logger.warning("Impossible de vérifier l'espace disque : %s", exc)
            return True  # on ne bloque pas l'enregistrement sur une erreur de check

        if libre_go < self.config.espace_disque_min_go:
            self.logger.error(
                "Espace disque insuffisant (%.2f Go restants, seuil %.2f Go). "
                "Enregistrement suspendu.",
                libre_go,
                self.config.espace_disque_min_go,
            )
            return False
        return True

    def _purger_anciens_enregistrements(self) -> None:
        if self.config.retention_jours <= 0:
            return

        limite = time.time() - self.config.retention_jours * 86400
        for dossier_date in self.dossier_dvr_base.iterdir():
            if not dossier_date.is_dir():
                continue
            try:
                date_dossier = datetime.strptime(dossier_date.name, "%Y-%m-%d")
            except ValueError:
                continue
            if date_dossier.timestamp() < limite:
                shutil.rmtree(dossier_date, ignore_errors=True)
                self.logger.info("Purge : dossier expiré supprimé (%s)", dossier_date.name)

    # ------------------------------------------------------------------ #
    # Enregistrement
    # ------------------------------------------------------------------ #

    def _demarrer_enregistrement(self, frame) -> None:
        if not self._espace_disque_suffisant():
            return

        date_du_jour = datetime.now().strftime("%Y-%m-%d")
        dossier_du_jour = self.dossier_dvr_base / date_du_jour
        dossier_du_jour.mkdir(parents=True, exist_ok=True)

        horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
        nom_fichier = dossier_du_jour / f"{self.config.nom_zone}_{horodatage}.avi"

        fourcc = cv2.VideoWriter_fourcc(*self.config.codec)
        hauteur, largeur = frame.shape[:2]

        try:
            self.video_writer = cv2.VideoWriter(
                str(nom_fichier), fourcc, self.config.fps_sortie, (largeur, hauteur)
            )
            if not self.video_writer.isOpened():
                raise IOError("VideoWriter n'a pas pu s'ouvrir (codec/chemin invalide ?)")
        except Exception as exc:
            self.logger.error("Échec de création du fichier vidéo %s : %s", nom_fichier, exc)
            self.video_writer = None
            return

        self.en_train_d_enregistrer = True
        self.fichier_en_cours = nom_fichier
        self.logger.info("🔴 Mouvement détecté — début d'enregistrement : %s", nom_fichier)

    def _arreter_enregistrement(self, raison: str = "calme revenu") -> None:
        if self.video_writer is not None:
            self.video_writer.release()
        self.en_train_d_enregistrer = False
        self.logger.info("💾 Enregistrement clôturé (%s) : %s", raison, self.fichier_en_cours)
        self.video_writer = None
        self.fichier_en_cours = None

    # ------------------------------------------------------------------ #
    # Boucle principale
    # ------------------------------------------------------------------ #

    def démarrer(self) -> None:
        self.cam_stream = CameraStream(self.config, self.logger)
        time.sleep(1.0)

        back_sub = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=40, detectShadows=True
        )

        self.logger.info("Système DVR actif sur la zone : %s", self.config.nom_zone)
        self.logger.info(
            "Mode : %s | Quitter avec %s",
            "headless" if self.config.headless else "affichage",
            "Ctrl+C" if self.config.headless else "la touche 'Q' ou Ctrl+C",
        )

        derniere_purge = 0.0

        try:
            while not self._arret_demande.is_set():
                if self.cam_stream and not self.cam_stream.est_actif():
                    self.logger.error("Flux caméra définitivement indisponible. Arrêt du système.")
                    break

                ret, frame = self.cam_stream.lire_frame()
                if not ret or frame is None:
                    time.sleep(0.05)
                    continue

                temps_actuel = time.time()

                # Purge des vieux enregistrements, une fois par heure
                if temps_actuel - derniere_purge > 3600:
                    self._purger_anciens_enregistrements()
                    derniere_purge = temps_actuel

                seuil_surface = self.ajuster_sensibilite()

                masque = back_sub.apply(frame)
                _, thresh = cv2.threshold(masque, 254, 255, cv2.THRESH_BINARY)
                contours, _ = cv2.findContours(
                    thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )

                mouvement_detecte = any(
                    cv2.contourArea(c) > seuil_surface for c in contours
                )

                if mouvement_detecte:
                    self.dernier_mouvement_temps = temps_actuel
                    if not self.en_train_d_enregistrer:
                        self._demarrer_enregistrement(frame)

                if self.en_train_d_enregistrer:
                    if self.video_writer is not None:
                        self.video_writer.write(frame)

                    if temps_actuel - self.dernier_mouvement_temps > self.config.duree_prolongation:
                        self._arreter_enregistrement()

                if not self.config.headless:
                    statut = "ENREGISTREMENT" if self.en_train_d_enregistrer else "VEILLE"
                    couleur = (0, 0, 255) if self.en_train_d_enregistrer else (0, 255, 0)
                    cv2.putText(
                        frame,
                        f"Cam: {self.config.nom_zone} | Statut: {statut} | Seuil: {seuil_surface}",
                        (15, 35),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        couleur,
                        2,
                    )
                    cv2.imshow(f"Moniteur DVR - {self.config.nom_zone}", frame)

                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

        finally:
            self._arreter_proprement()

    def _arreter_proprement(self) -> None:
        if self.en_train_d_enregistrer:
            self._arreter_enregistrement(raison="arrêt du programme")
        if self.cam_stream is not None:
            self.cam_stream.arreter()
        if not self.config.headless:
            cv2.destroyAllWindows()
        self.logger.info("Système DVR arrêté proprement.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parser_arguments() -> DVRConfig:
    parser = argparse.ArgumentParser(
        description="Système DVR de détection de mouvement (caméra locale ou RTSP)."
    )
    parser.add_argument(
        "--source", required=True,
        help="Index de caméra locale (ex: 0) ou URL RTSP (ex: rtsp://user:pass@ip)."
    )
    parser.add_argument("--zone", default="Camera_1", help="Nom de la zone/caméra.")
    parser.add_argument("--sortie", default="enregistrements", help="Dossier de sortie des vidéos.")
    parser.add_argument("--duree-prolongation", type=float, default=5.0,
                         help="Secondes sans mouvement avant d'arrêter l'enregistrement.")
    parser.add_argument("--fps", type=float, default=20.0, help="FPS du fichier de sortie.")
    parser.add_argument("--codec", default="XVID", help="Codec FOURCC (ex: XVID, MJPG, mp4v).")
    parser.add_argument("--headless", action="store_true",
                         help="Désactive l'affichage (pour serveur / service en arrière-plan).")
    parser.add_argument("--retention-jours", type=int, default=30,
                         help="Nombre de jours avant purge automatique (0 = désactivé).")
    parser.add_argument("--espace-min-go", type=float, default=1.0,
                         help="Espace disque minimal (Go) avant suspension de l'enregistrement.")
    args = parser.parse_args()

    source: str | int = args.source
    if isinstance(source, str) and source.isdigit():
        source = int(source)

    return DVRConfig(
        source=source,
        nom_zone=args.zone,
        dossier_sortie=args.sortie,
        duree_prolongation=args.duree_prolongation,
        fps_sortie=args.fps,
        codec=args.codec,
        headless=args.headless,
        retention_jours=args.retention_jours,
        espace_disque_min_go=args.espace_min_go,
    )


def main() -> int:
    config = parser_arguments()
    systeme = StadeEnregistreurMouvement(config)
    try:
        systeme.démarrer()
    except Exception as exc:  # pragma: no cover - filet de sécurité
        logging.getLogger("dvr").exception("Erreur fatale non gérée : %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
