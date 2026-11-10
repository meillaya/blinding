"""Tests for the pinned downloader: hash check, atomic replace, cache reuse."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from blinding.download import cache_path, download_file, verify_file
from blinding.errors import MissingDependency
from blinding.manifest import Model, ModelFile

PAYLOAD = b"a pinned model file" * 40
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _model(sha: str = SHA, size: int | None = None) -> tuple[Model, ModelFile]:
    model_file = ModelFile(
        quant="q4_0",
        shard=1,
        file="tiny.gguf",
        sha256=sha,
        size=len(PAYLOAD) if size is None else size,
    )
    model = Model(
        id="tiny",
        repo="owner/repo",
        revision="a" * 40,
        context=512,
        default_quant="q4_0",
        files=(model_file,),
    )
    return (model, model_file)


def _client(payload: bytes = PAYLOAD) -> httpx.Client:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    return httpx.Client(transport=transport, follow_redirects=True)


def test_download_writes_a_verified_file(tmp_path: Path) -> None:
    """A good payload lands at the content-addressed path."""
    model, model_file = _model()
    progress: list[tuple[str, int, int]] = []
    client = _client()
    try:
        path = download_file(
            model,
            model_file,
            tmp_path,
            progress=lambda name, written, total: progress.append((name, written, total)),
            client=client,
        )
    finally:
        client.close()
    assert path == cache_path(tmp_path, model_file)
    assert path.read_bytes() == PAYLOAD
    assert verify_file(path, model_file)
    assert progress[-1][1] == len(PAYLOAD)


def test_download_rejects_a_bad_hash(tmp_path: Path) -> None:
    """A mismatched hash deletes the partial file and raises."""
    model, model_file = _model(sha="b" * 64)
    client = _client()
    try:
        with pytest.raises(MissingDependency, match="sha256"):
            download_file(model, model_file, tmp_path, client=client)
    finally:
        client.close()
    assert not cache_path(tmp_path, model_file).exists()
    assert not list(cache_path(tmp_path, model_file).parent.glob("*.part"))


def test_download_reuses_a_verified_file(tmp_path: Path) -> None:
    """A cached file is hashed, not fetched again."""
    model, model_file = _model()
    client = _client()
    try:
        first = download_file(model, model_file, tmp_path, client=client)
        second = download_file(model, model_file, tmp_path, client=client)
    finally:
        client.close()
    assert first == second
    assert second.read_bytes() == PAYLOAD


def test_ensure_model_links_shards_side_by_side(tmp_path: Path) -> None:
    """Split models need every shard in one directory, not one directory each."""
    from blinding.download import ensure_model

    shards = [
        ModelFile(
            quant="q4_k_m",
            shard=index,
            file=f"tiny-{index:05d}-of-00002.gguf",
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
        for index, payload in ((1, PAYLOAD), (2, PAYLOAD + b"second"))
    ]
    model = Model(
        id="tiny",
        repo="owner/repo",
        revision="a" * 40,
        context=512,
        default_quant="q4_k_m",
        files=tuple(shards),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(200, content=PAYLOAD if "00001" in name else PAYLOAD + b"second")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        paths = ensure_model(model, "q4_k_m", tmp_path, client=client)
    finally:
        client.close()
    assert paths[0].parent == paths[1].parent
    assert paths[0].parent.name == "q4_k_m"
    assert paths[0].parent.parent.name == "tiny"
