"""Latency gate: fail if p95 in-process predict latency exceeds 100 ms.

Standalone: ``uv run python evals/latency_gate.py``.

Approach: load the trained artifact, warm the interpreter/model up, then time
``N_MEASUREMENTS`` calls of ``model.predict`` over the full golden feature
matrix — one timed call models one batched inference request, mirroring the
model-server ``/v1/predict`` batch path. Measuring in-process keeps the gate
deterministic and CI-friendly (no server, no network); the GradientBoosting
predict is single-threaded and value-independent, so timings are stable. To
gate a live deployment instead, point the same timing loop at the running
server's ``/v1/predict`` endpoint.
"""

from __future__ import annotations

import sys
import time

import numpy as np
from eval_common import EvalError, load_golden_dataset, load_trained_model
from numpy.typing import NDArray
from sklearn_seam import GradientBoostingClassifier, predict_labels

P95_THRESHOLD_MS = 100.0
N_MEASUREMENTS = 100
WARMUP_ITERATIONS = 10


def measure_p95_latency_ms(
    model: GradientBoostingClassifier,
    features: NDArray[np.float64],
) -> float:
    """Return the p95 latency (ms) of N batched predict calls after warmup."""
    for _ in range(WARMUP_ITERATIONS):
        predict_labels(model, features)
    latencies_ms: list[float] = []
    for _ in range(N_MEASUREMENTS):
        start = time.perf_counter_ns()
        predict_labels(model, features)
        latencies_ms.append((time.perf_counter_ns() - start) / 1e6)
    return float(np.percentile(np.asarray(latencies_ms, dtype=np.float64), 95))


def main() -> None:
    model = load_trained_model()
    golden = load_golden_dataset()

    p95_ms = measure_p95_latency_ms(model, golden.features)
    print(
        f"p95 latency over {N_MEASUREMENTS} batched predictions "
        f"({golden.n_rows} rows each): {p95_ms:.3f} ms"
    )
    if p95_ms > P95_THRESHOLD_MS:
        print(
            f"LATENCY GATE FAILED: p95 {p95_ms:.3f} ms > {P95_THRESHOLD_MS:.1f} ms",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"LATENCY GATE PASSED: p95 {p95_ms:.3f} ms <= {P95_THRESHOLD_MS:.1f} ms")


if __name__ == "__main__":
    try:
        main()
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
