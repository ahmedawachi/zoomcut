"""Entry point for the packaged Zoomcut executable.

Running it with no arguments opens the app; anything else behaves exactly like
the `zoomcut` command line.
"""
import multiprocessing
import sys

from zoomcut.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()     # harmless elsewhere, required on Windows
    sys.exit(main())
