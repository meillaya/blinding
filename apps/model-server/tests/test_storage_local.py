"""LocalStorageBackend tests: metadata-driven resolution, env override, listing.

Resolution convention under test: artifacts are matched by the ``model_id`` /
``model_version`` recorded in ``model_meta.json``, NOT by directory name — so
``fraud-detector`` (hyphen) resolves under ``fraud_detector/`` (underscore).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from inferenceops_model_server.errors import ModelNotFound
from inferenceops_model_server.schemas import ModelInfo
from inferenceops_model_server.storage.local import (
    ARTIFACT_FILENAME,
    DEFAULT_MODELS_DIR,
    LocalStorageBackend,
)

FRAUD_DETECTOR: Final = ModelInfo(model_id="fraud-detector", version="1.0.0")
_ARTIFACTS_ROOT: Final = Path(__file__).resolve().parents[3] / "models" / "artifacts"


def write_artifact(root: Path, dirname: str, info: ModelInfo) -> Path:
    """Create ``<root>/<dirname>/{model_meta.json,model.pkl}``; return the directory."""
    directory = root / dirname
    directory.mkdir(parents=True)
    meta = {"model_id": info.model_id, "model_version": info.version}
    (directory / "model_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (directory / ARTIFACT_FILENAME).write_bytes(b"placeholder")
    return directory


def test_get_artifact_resolves_by_metadata_not_directory_name(tmp_path: Path) -> None:
    directory = write_artifact(tmp_path, "fraud_detector", FRAUD_DETECTOR)
    backend = LocalStorageBackend(tmp_path)
    assert backend.get_artifact("fraud-detector", "1.0.0") == directory / ARTIFACT_FILENAME


def test_get_artifact_raises_for_unknown_model(tmp_path: Path) -> None:
    write_artifact(tmp_path, "fraud_detector", FRAUD_DETECTOR)
    with pytest.raises(ModelNotFound):
        LocalStorageBackend(tmp_path).get_artifact("no-such-model", "9.9.9")


def test_get_artifact_raises_for_wrong_version(tmp_path: Path) -> None:
    write_artifact(tmp_path, "fraud_detector", FRAUD_DETECTOR)
    with pytest.raises(ModelNotFound):
        LocalStorageBackend(tmp_path).get_artifact("fraud-detector", "2.0.0")


def test_get_artifact_raises_when_pkl_missing(tmp_path: Path) -> None:
    directory = write_artifact(tmp_path, "fraud_detector", FRAUD_DETECTOR)
    (directory / ARTIFACT_FILENAME).unlink()
    with pytest.raises(ModelNotFound):
        LocalStorageBackend(tmp_path).get_artifact("fraud-detector", "1.0.0")


def test_malformed_meta_is_ignored(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "model_meta.json").write_text("{not json", encoding="utf-8")
    directory = write_artifact(tmp_path, "good", FRAUD_DETECTOR)
    backend = LocalStorageBackend(tmp_path)
    assert backend.get_artifact("fraud-detector", "1.0.0") == directory / ARTIFACT_FILENAME
    assert backend.list_models() == [FRAUD_DETECTOR]


def test_list_models_sorted_by_id_then_version(tmp_path: Path) -> None:
    write_artifact(tmp_path, "b", ModelInfo(model_id="b-model", version="1.0.0"))
    write_artifact(tmp_path, "a2", ModelInfo(model_id="a-model", version="2.0.0"))
    write_artifact(tmp_path, "a1", ModelInfo(model_id="a-model", version="1.0.0"))
    assert LocalStorageBackend(tmp_path).list_models() == [
        ModelInfo(model_id="a-model", version="1.0.0"),
        ModelInfo(model_id="a-model", version="2.0.0"),
        ModelInfo(model_id="b-model", version="1.0.0"),
    ]


def test_list_models_empty_when_root_missing(tmp_path: Path) -> None:
    assert LocalStorageBackend(tmp_path / "absent").list_models() == []


def test_get_artifact_raises_when_root_missing(tmp_path: Path) -> None:
    with pytest.raises(ModelNotFound):
        LocalStorageBackend(tmp_path / "absent").get_artifact("fraud-detector", "1.0.0")


def test_default_root_points_at_repo_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODELS_DIR", raising=False)
    assert DEFAULT_MODELS_DIR == _ARTIFACTS_ROOT
    assert LocalStorageBackend().get_artifact("fraud-detector", "1.0.0").is_file()


def test_env_var_overrides_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    write_artifact(tmp_path, "env_model", ModelInfo(model_id="env-model", version="0.1.0"))
    assert LocalStorageBackend().list_models() == [
        ModelInfo(model_id="env-model", version="0.1.0")
    ]


def test_explicit_root_wins_over_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "env-root"))
    explicit = tmp_path / "explicit-root"
    write_artifact(explicit, "m", ModelInfo(model_id="explicit-model", version="1.0.0"))
    assert LocalStorageBackend(explicit).list_models() == [
        ModelInfo(model_id="explicit-model", version="1.0.0")
    ]


def test_blank_env_var_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELS_DIR", "   ")
    assert LocalStorageBackend().get_artifact("fraud-detector", "1.0.0").is_file()
