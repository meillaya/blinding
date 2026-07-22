"""Pytest bootstrap: make the src-layout app packages importable at runtime.

Each app is a uv workspace member with ``package = false`` (run from source,
never installed), so its importable package lives under ``apps/<app>/src/``.
Prepending those source roots to ``sys.path`` lets the smoke test import
``inferenceops_gateway``, ``inferenceops_model_server`` and
``inferenceops_worker`` directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_ROOTS = (
    _REPO_ROOT / "apps" / "gateway" / "src",
    _REPO_ROOT / "apps" / "model-server" / "src",
    _REPO_ROOT / "apps" / "worker" / "src",
)

for _root in _SOURCE_ROOTS:
    _root_str = str(_root)
    if _root_str not in sys.path:
        sys.path.insert(0, _root_str)
