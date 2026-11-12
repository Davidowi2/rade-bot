"""Pytest bootstrap.

The package lives in ``src/`` and is not installed into the virtualenv, so the
suite only worked when a module that happened to call ``sys.path.insert`` was
collected first. Running a single file (``pytest tests/test_mirror_pipeline.py``)
failed with ``ModuleNotFoundError: No module named 'rade'``. Adding the path here
makes every invocation independent of collection order.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
