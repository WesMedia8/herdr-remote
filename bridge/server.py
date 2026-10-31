#!/usr/bin/env python3
"""Compatibility launcher for existing python bridge/server.py services."""
import os
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ.setdefault('HERD_CONFIG', str(Path(__file__).resolve().parent / 'config.json'))
    from herdr_remote_bridge.cli import main
else:
    from .herdr_remote_bridge.cli import main

if __name__ == '__main__':
    sys.exit(main())
