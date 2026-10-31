import os
import sys
from pathlib import Path

# Running from the repo (`python -m bridge ...`): use bridge/config.json, the same file
# `python bridge/server.py` uses, so pair/doctor/serve all agree on one config.
os.environ.setdefault('HERD_CONFIG', str(Path(__file__).resolve().parent / 'config.json'))

from .herdr_remote_bridge.cli import main

sys.exit(main())
