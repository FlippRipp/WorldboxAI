"""Put the module directory on sys.path so the relocated `wbworldgen` package
(worldgen / world_map / terrain) is importable when pytest collects the module's
own tests directly from this folder.
"""
import os
import sys

_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
if _MOD_DIR not in sys.path:
    sys.path.insert(0, _MOD_DIR)
