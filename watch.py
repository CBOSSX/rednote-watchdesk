"""Compatibility entry point for existing installations; use the package module."""
import sys
from rednote_watchdesk.watch import main

if __name__ == "__main__":
    sys.exit(main() or 0)
