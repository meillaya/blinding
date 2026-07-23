"""Pytest bootstrap for worker tests.

Both the worker and the model-server are uv workspace members with
``package = false`` (run from source, never installed); prepend their src
roots so ``inferenceops_worker`` and its reuse of the model-server's
``MetricsRegistry`` (drift_job publishes ``inferenceops_drift_score``) import.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_SOURCE_ROOTS: Final = (
    _REPO_ROOT / "apps" / "worker" / "src",
    _REPO_ROOT / "apps" / "model-server" / "src",
)

for _source_root in _SOURCE_ROOTS:
    _source_root_str = str(_source_root)
    if _source_root_str not in sys.path:
        sys.path.insert(0, _source_root_str)
