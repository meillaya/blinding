"""Shared fixtures for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def fake_server() -> Path:
    """Path to the fake llama-server executable."""
    return FAKES / "fake_llama_server.py"


@pytest.fixture
def repo_root() -> Path:
    """The checkout root."""
    return ROOT
