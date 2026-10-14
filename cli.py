"""Point d'entrée en ligne de commande pour le paquet camera_dvr."""

import sys

from .dvr import main as _main


def main() -> None:
    sys.exit(_main())


if __name__ == "__main__":
    main()
