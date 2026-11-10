"""Tests for the model manifest and its integrity rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from blinding.errors import ManifestError
from blinding.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_manifest_loads() -> None:
    """The real manifest resolves a single file and a sharded quant."""
    loaded = load_manifest(ROOT / "models.toml")
    model, quant = loaded.resolve("qwen2.5-0.5b-instruct:q4_k_m")
    assert quant == "q4_k_m"
    assert len(model.files_for(quant)) == 1
    sharded, sharded_quant = loaded.resolve("qwen2.5-7b-instruct:q4_k_m")
    assert [item.shard for item in sharded.files_for(sharded_quant)] == [1, 2]


def test_default_quant_is_used_without_a_suffix() -> None:
    """A bare model id resolves to its declared default quant."""
    loaded = load_manifest(ROOT / "models.toml")
    model, quant = loaded.resolve("stories260k-f32")
    assert model.id == "stories260k-f32"
    assert quant == "f32"


def test_unknown_quant_lists_the_known_values() -> None:
    """A bad quant names the available ones."""
    loaded = load_manifest(ROOT / "models.toml")
    with pytest.raises(ManifestError, match="known quants"):
        loaded.resolve("qwen2.5-0.5b-instruct:q2_k")


def _write_manifest(tmp_path: Path, revision: str, sha256: str) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(
        f"""
[[models]]
id = "tiny"
repo = "owner/repo"
revision = "{revision}"
context = 512
default_quant = "q4_0"

[[models.files]]
quant = "q4_0"
shard = 1
file = "tiny.gguf"
sha256 = "{sha256}"
size = 10
""",
        encoding="utf-8",
    )
    return path


def test_revision_must_be_a_commit_hash(tmp_path: Path) -> None:
    """`main` and short hashes are rejected before a download."""
    path = _write_manifest(tmp_path, "main", "a" * 64)
    with pytest.raises(ManifestError, match="revision"):
        load_manifest(path)


def test_sha256_must_be_full_width(tmp_path: Path) -> None:
    """A truncated hash is a manifest error, not a warning."""
    path = _write_manifest(tmp_path, "a" * 40, "abc")
    with pytest.raises(ManifestError, match="sha256"):
        load_manifest(path)
