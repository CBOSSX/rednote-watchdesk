"""Code and private per-user runtime data always live in separate directories."""
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parent

def default_home():
    if sys.platform == 'darwin':
        return Path.home()/'Library/Application Support/RednoteWatchdesk'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home()/'.local/share'))/'rednote-watchdesk'

RUNTIME = Path(os.environ.get('REDNOTE_WATCHDESK_HOME', default_home())).expanduser().resolve()
if RUNTIME == PROJECT or PROJECT in RUNTIME.parents:
    raise RuntimeError('REDNOTE_WATCHDESK_HOME must be outside the source repository')
HEALTH = RUNTIME/'health/data'
MONITOR_LABEL = 'app.rednote-watchdesk.monitor'
