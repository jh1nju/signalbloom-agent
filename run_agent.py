"""Entry script for the Windows exe (PyInstaller). Same as python -m signalbloom_agent."""

import sys

from signalbloom_agent.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
