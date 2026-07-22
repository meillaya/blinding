"""Model artifact storage backends (stub)."""

from inferenceops_model_server.storage.base import ModelStorage
from inferenceops_model_server.storage.local import LocalStorageBackend

__all__ = ["LocalStorageBackend", "ModelStorage"]
