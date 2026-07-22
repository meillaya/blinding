"""Model loader tests: real artifact load, cache identity, load timing, ModelNotFound.

Happy-path tests load the REAL todo-6 artifact (seeded, deterministic) through
``LocalStorageBackend`` — no mocks on the load path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from inferenceops_model_server import model_loader
from inferenceops_model_server.errors import ModelNotFound
from inferenceops_model_server.storage.local import LocalStorageBackend

MODEL_ID: Final = "fraud-detector"
MODEL_VERSION: Final = "1.0.0"
FEATURE_COUNT: Final = 20
_ARTIFACTS_ROOT: Final = Path(__file__).resolve().parents[3] / "models" / "artifacts"


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    """Isolate every test from the module-level model cache."""
    model_loader.clear_cache()
    yield
    model_loader.clear_cache()


def test_load_model_returns_usable_model() -> None:
    model = model_loader.load_model(
        LocalStorageBackend(_ARTIFACTS_ROOT), MODEL_ID, MODEL_VERSION
    )
    assert model.n_features_in_ == FEATURE_COUNT


def test_load_model_returns_same_cached_object_on_repeated_loads() -> None:
    storage = LocalStorageBackend(_ARTIFACTS_ROOT)
    first = model_loader.load_model(storage, MODEL_ID, MODEL_VERSION)
    second = model_loader.load_model(storage, MODEL_ID, MODEL_VERSION)
    assert first is second


def test_load_record_exposes_load_seconds() -> None:
    storage = LocalStorageBackend(_ARTIFACTS_ROOT)
    model = model_loader.load_model(storage, MODEL_ID, MODEL_VERSION)
    record = model_loader.load_record(MODEL_ID, MODEL_VERSION)
    assert record is not None
    assert record.model is model
    assert record.load_seconds > 0.0


def test_load_record_is_none_before_load() -> None:
    assert model_loader.load_record("never-loaded", "0.0.0") is None


def test_load_model_raises_model_not_found_for_unknown_model() -> None:
    storage = LocalStorageBackend(_ARTIFACTS_ROOT)
    with pytest.raises(ModelNotFound) as excinfo:
        model_loader.load_model(storage, "no-such-model", "9.9.9")
    assert excinfo.value.model_id == "no-such-model"
    assert excinfo.value.version == "9.9.9"


def test_clear_cache_forces_a_fresh_load() -> None:
    storage = LocalStorageBackend(_ARTIFACTS_ROOT)
    first = model_loader.load_model(storage, MODEL_ID, MODEL_VERSION)
    model_loader.clear_cache()
    second = model_loader.load_model(storage, MODEL_ID, MODEL_VERSION)
    assert first is not second
