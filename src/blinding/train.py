"""Fine-tuning through llama.cpp's own finetune example.

The upstream example is work in progress and needs an F32 GGUF. Blinding keeps
the test small and honest: a 1.2 MB model, a short public-domain corpus, one
number for training loss, and perplexity before and after on held-out text. A
run that cannot produce those numbers is a failed run, not a result.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from blinding import proc
from blinding.errors import MissingDependency, UpstreamError

LOSS_PATTERN = re.compile(r"loss\s*=\s*([0-9.]+)", re.IGNORECASE)
PPL_PATTERN = re.compile(r"Final estimate:\s*PPL\s*=\s*([0-9.]+)", re.IGNORECASE)
DEFAULT_EPOCHS = 1
DEFAULT_CTX = 512
DEFAULT_TIMEOUT_S = 1800.0
TRAIN_FRACTION = 0.9


@dataclass(frozen=True, slots=True)
class TrainConfig:
    """Inputs for one finetune run."""

    model_path: Path
    corpus: Path
    output_path: Path
    work_dir: Path
    epochs: int = DEFAULT_EPOCHS
    ctx_size: int = DEFAULT_CTX
    batch_size: int = DEFAULT_CTX
    ubatch_size: int = DEFAULT_CTX
    gpu_layers: int = 0
    learning_rate: float | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class TrainResult:
    """Measured outcome of one finetune run."""

    ok: bool
    train_log: Path
    output_path: Path
    before_ppl: float | None
    after_ppl: float | None
    last_loss: float | None
    duration_s: float
    error: str

    def as_dict(self) -> dict[str, object]:
        """Plain mapping for the run record."""
        return {
            "ok": self.ok,
            "train_log": str(self.train_log),
            "output_path": str(self.output_path),
            "before_ppl": self.before_ppl,
            "after_ppl": self.after_ppl,
            "last_loss": self.last_loss,
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
        }


def split_corpus(corpus: Path, work_dir: Path) -> tuple[Path, Path]:
    """Split a text corpus into train and held-out files by line count."""
    text = corpus.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) < 10:
        raise MissingDependency(f"corpus {corpus} needs at least ten lines")
    cut = max(1, int(len(lines) * TRAIN_FRACTION))
    train_path = work_dir / "train-corpus.txt"
    heldout_path = work_dir / "heldout-corpus.txt"
    train_path.write_text("\n".join(lines[:cut]) + "\n", encoding="utf-8")
    heldout_path.write_text("\n".join(lines[cut:]) + "\n", encoding="utf-8")
    return train_path, heldout_path


def _run_logged(argv: list[str], log_path: Path, timeout_s: float) -> tuple[int, str]:
    started = time.perf_counter()
    with log_path.open("wb") as handle:
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UpstreamError(
                f"{Path(argv[0]).name} exceeded {timeout_s:g}s; log: {log_path}"
            ) from exc
    elapsed = time.perf_counter() - started
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return completed.returncode, text + f"\n[elapsed {elapsed:.1f}s]\n"


def perplexity(tools: proc.LlamaTools, model_path: Path, corpus: Path, ctx_size: int) -> float:
    """Perplexity of one text file under one model."""
    binary = tools.require("llama-perplexity")
    argv = [
        binary,
        "--model",
        str(model_path),
        "--file",
        str(corpus),
        "--ctx-size",
        str(ctx_size),
        "--threads",
        str(os.cpu_count() or 1),
    ]
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_S,
        check=False,
    )
    if completed.returncode != 0:
        raise UpstreamError(f"llama-perplexity failed: {completed.stderr[-400:]}")
    match = PPL_PATTERN.search(completed.stdout + completed.stderr)
    if match is None:
        raise UpstreamError("llama-perplexity printed no final estimate")
    return float(match.group(1))


def finetune(tools: proc.LlamaTools, config: TrainConfig) -> TrainResult:
    """Run the finetune, then score the result against the base model."""
    backend = tools.require("llama-finetune")
    config.work_dir.mkdir(parents=True, exist_ok=True)
    train_path, heldout_path = split_corpus(config.corpus, config.work_dir)
    log_path = config.work_dir / "finetune.log"
    argv = [
        backend,
        "--model",
        str(config.model_path),
        "--file",
        str(train_path),
        "--ctx-size",
        str(config.ctx_size),
        "--batch-size",
        str(config.batch_size),
        "--ubatch-size",
        str(config.ubatch_size),
        "--n-gpu-layers",
        str(config.gpu_layers),
        "--epochs",
        str(config.epochs),
        "-o",
        str(config.output_path),
    ]
    if config.learning_rate is not None:
        argv.extend(["--learning-rate", str(config.learning_rate)])

    started = time.perf_counter()
    before = perplexity(tools, config.model_path, heldout_path, config.ctx_size)
    code, train_text = _run_logged(argv, log_path, config.timeout_s)
    duration = time.perf_counter() - started
    if code != 0 or not config.output_path.is_file():
        return TrainResult(
            ok=False,
            train_log=log_path,
            output_path=config.output_path,
            before_ppl=before,
            after_ppl=None,
            last_loss=_last_loss(train_text),
            duration_s=duration,
            error=f"llama-finetune exited {code}",
        )
    after = perplexity(tools, config.output_path, heldout_path, config.ctx_size)
    return TrainResult(
        ok=True,
        train_log=log_path,
        output_path=config.output_path,
        before_ppl=before,
        after_ppl=after,
        last_loss=_last_loss(train_text),
        duration_s=duration,
        error="",
    )


def _last_loss(text: str) -> float | None:
    matches = LOSS_PATTERN.findall(text)
    return float(matches[-1]) if matches else None
