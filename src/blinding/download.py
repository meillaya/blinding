"""Pinned model downloads with hash verification and atomic replacement.

A download lands in a temporary file, is hashed while it streams, and is moved
into place only when both the size and the SHA-256 match the manifest entry. A
failed download leaves the cache exactly as it found it.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import httpx

from blinding.errors import MissingDependency, UpstreamError
from blinding.manifest import Model, ModelFile

CHUNK_BYTES = 1 << 20
CONNECT_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 120.0
LOCK_SUFFIX = ".lock"
PART_SUFFIX = ".part"
HUGGING_FACE_BASE = "https://huggingface.co"

Progress = Callable[[str, int, int], None]


def default_cache() -> Path:
    """Cache directory from BLINDING_CACHE, or the user cache dir."""
    raw = os.environ.get("BLINDING_CACHE", "")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "blinding"


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path: Path, model_file: ModelFile) -> bool:
    """True when the file exists, has the pinned size, and hashes correctly."""
    try:
        if path.stat().st_size != model_file.size:
            return False
    except OSError:
        return False
    return sha256_file(path) == model_file.sha256


def download_url(model: Model, model_file: ModelFile, base: str = HUGGING_FACE_BASE) -> str:
    """Resolve URL for one pinned file at one commit."""
    return f"{base}/{model.repo}/resolve/{model.revision}/{quote(model_file.file)}"


def cache_path(cache: Path, model_file: ModelFile) -> Path:
    """Content-addressed path for one file."""
    return cache / "models" / model_file.sha256 / model_file.file


def quant_dir(cache: Path, model_id: str, quant: str) -> Path:
    """Directory that holds one quantisation with every shard side by side.

    llama.cpp finds a split model's other shards by filename in the directory of
    the first shard, so content-addressed files have to be linked together
    before the server sees them.
    """
    return cache / "models" / model_id / quant


def _download_once(
    model: Model,
    model_file: ModelFile,
    target: Path,
    progress: Progress | None,
    client: httpx.Client,
) -> None:
    part = target.with_name(target.name + PART_SUFFIX)
    part.unlink(missing_ok=True)
    digest = hashlib.sha256()
    written = 0
    url = download_url(model, model_file)
    try:
        with client.stream("GET", url) as response:
            if response.status_code != httpx.codes.OK:
                raise UpstreamError(f"{url}: HTTP {response.status_code}")
            with part.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(model_file.file, written, model_file.size)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise UpstreamError(f"{url}: {exc}") from exc
    except OSError:
        part.unlink(missing_ok=True)
        raise

    actual = digest.hexdigest()
    if written != model_file.size or actual != model_file.sha256:
        part.unlink(missing_ok=True)
        raise MissingDependency(
            f"{model_file.file}: expected {model_file.size} bytes with sha256 "
            f"{model_file.sha256}, got {written} bytes with sha256 {actual}"
        )
    os.replace(part, target)


def download_file(
    model: Model,
    model_file: ModelFile,
    cache: Path,
    *,
    progress: Progress | None = None,
    client: httpx.Client | None = None,
) -> Path:
    """Download one file unless a verified copy is already cached."""
    target = cache_path(cache, model_file)
    if target.exists() and verify_file(target, model_file):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + LOCK_SUFFIX)
    owned_client = client is None
    active_client = (
        client
        if client is not None
        else httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
        )
    )
    try:
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if target.exists() and verify_file(target, model_file):
                return target
            _download_once(model, model_file, target, progress, active_client)
    finally:
        if owned_client:
            active_client.close()
        lock_path.unlink(missing_ok=True)
    return target


def ensure_model(
    model: Model,
    quant: str,
    cache: Path,
    *,
    progress: Progress | None = None,
    client: httpx.Client | None = None,
) -> tuple[Path, ...]:
    """Download and verify every shard of one quantisation, in order."""
    downloaded: list[tuple[ModelFile, Path]] = []
    for model_file in model.files_for(quant):
        downloaded.append(
            (
                model_file,
                download_file(model, model_file, cache, progress=progress, client=client),
            )
        )

    layout = quant_dir(cache, model.id, quant)
    layout.mkdir(parents=True, exist_ok=True)
    linked: list[Path] = []
    for model_file, target in downloaded:
        destination = layout / model_file.file
        if destination.exists() and destination.resolve() == target.resolve():
            linked.append(destination)
            continue
        if destination.exists():
            destination.unlink()
        try:
            os.link(target, destination)
        except OSError:
            import shutil

            shutil.copyfile(target, destination)
        if not verify_file(destination, model_file):
            raise MissingDependency(f"{destination}: linked copy failed verification")
        linked.append(destination)
    return tuple(linked)
