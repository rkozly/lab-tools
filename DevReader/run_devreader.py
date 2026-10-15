"""Launcher script (used by PyInstaller and for running without installing)."""

import sys

from devreader.app import main

if __name__ == "__main__":
    sys.exit(main())
