"""Pytest bootstrap for model-server tests.

The app is a uv workspace member with ``package = false`` (run from source,
never installed), so its importable package lives under ``apps/model-server/
src/``; prepend that root to ``sys.path``. Also guarantee the real todo-6
fraud-detector artifact exists before any test loads it, training it when
``model.pkl`` is absent (``model.pkl`` is git-ignored, ``model_meta.json`` is
tracked), so the suite never depends on a stale or missing artifact.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_SOURCE_ROOT: Final = _REPO_ROOT / "apps" / "model-server" / "src"
_TRAIN_SCRIPT: Final = _REPO_ROOT / "models" / "training" / "train.py"
ARTIFACT_DIR: Final = _REPO_ROOT / "models" / "artifacts" / "fraud_detector"

_source_root_str = str(_SOURCE_ROOT)
if _source_root_str not in sys.path:
    sys.path.insert(0, _source_root_str)


@pytest.fixture(scope="session", autouse=True)
def trained_artifact() -> Path:
    """Ensure the real todo-6 artifact exists; train it if ``model.pkl`` is absent."""
    model_pkl = ARTIFACT_DIR / "model.pkl"
    if not model_pkl.is_file():
        subprocess.run(
            [sys.executable, str(_TRAIN_SCRIPT)],
            check=True,
            capture_output=True,
            timeout=300,
        )
    return model_pkl
