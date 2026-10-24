"""Entrypoint for python3 -m k2y <cmd>."""
import sys
from k2y.cli import main

if __name__ == "__main__":
    sys.exit(main())
