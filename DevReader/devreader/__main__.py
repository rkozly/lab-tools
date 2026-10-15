"""Entry point for ``python -m devreader`` and the packaged executable."""

import sys

from devreader.app import main

if __name__ == "__main__":
    sys.exit(main())
