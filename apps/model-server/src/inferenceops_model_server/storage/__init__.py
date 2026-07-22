"""Model artifact storage backends."""

from inferenceops_model_server.storage.base import ModelStorage
from inferenceops_model_server.storage.local import LocalStorageBackend
from inferenceops_model_server.storage.s3 import S3StorageBackend

__all__ = ["LocalStorageBackend", "ModelStorage", "S3StorageBackend"]
