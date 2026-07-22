"""Model artifact loading and caching."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from inferenceops_model_server._sklearn_io import TrainedModel as TrainedModel
from inferenceops_model_server._sklearn_io import load_classifier

if TYPE_CHECKING:
    from inferenceops_model_server.storage.base import ModelStorage


@dataclass(frozen=True, slots=True)
class LoadRecord:
    """A cached model plus the wall-clock seconds its load took."""

    model: TrainedModel
    load_seconds: float


# Versioned artifacts are immutable, so a (model_id, version) key never needs
# invalidation. The lock makes concurrent first-loads load exactly once; it
# spans the whole get-or-load because this server loads one model at startup.
_CACHE: Final[dict[tuple[str, str], LoadRecord]] = {}
_LOCK: Final = threading.Lock()


def load_model(storage: ModelStorage, model_id: str, version: str) -> TrainedModel:
    """Load the artifact once and cache it; repeat calls return the same object.

    Raises:
        ModelNotFound: via ``storage`` when the artifact is absent.
    """
    key = (model_id, version)
    with _LOCK:
        record = _CACHE.get(key)
        if record is None:
            started = time.perf_counter()
            model = load_classifier(storage.get_artifact(model_id, version))
            record = LoadRecord(model=model, load_seconds=time.perf_counter() - started)
            _CACHE[key] = record
    return record.model


def load_record(model_id: str, version: str) -> LoadRecord | None:
    """Return the cached load record, or None if the model was never loaded."""
    return _CACHE.get((model_id, version))


def clear_cache() -> None:
    """Drop every cached model (test-isolation seam)."""
    _CACHE.clear()
