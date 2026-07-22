"""Coverage gate: enforce per-package line-coverage thresholds.

Standalone: ``uv run python scripts/coverage_gate.py`` (wired to
``make coverage-gate`` and the CI ``coverage`` job).

Runs the full test suite once with a fresh JSON coverage report, then parses
``coverage.json`` and asserts each package's line coverage (covered statements
/ statements) against its threshold:

    model-server >= 80%
    gateway      >= 70%

Per-package coverage is summed from the per-file statement counts under each
package's ``src/`` tree, so the packages are NEVER blended into one number —
a package below its floor fails the gate even if the other is at 100%. (This
repo's pytest collects the entire configured ``testpaths`` regardless of CLI
path args, so one run measures every package; scoping happens here, by prefix,
after the run.)

Exits non-zero with a clear message if any package is below threshold, if the
test suite itself fails, or if ``coverage.json`` is missing/empty/malformed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
COVERAGE_JSON: Final = REPO_ROOT / "coverage.json"


class CoverageGateError(Exception):
    """Raised when the coverage gate cannot measure or cannot pass."""


@dataclass(frozen=True, slots=True)
class PackageThreshold:
    """One gated package: its coverage.json path prefix and its floor."""

    name: str
    src_prefix: str
    min_line_coverage: float


THRESHOLDS: Final[tuple[PackageThreshold, ...]] = (
    PackageThreshold(
        name="model-server",
        src_prefix="apps/model-server/src/",
        min_line_coverage=80.0,
    ),
    PackageThreshold(
        name="gateway",
        src_prefix="apps/gateway/src/",
        min_line_coverage=70.0,
    ),
)


class FileSummary(BaseModel):
    """Per-file statement counts from coverage.json (extra fields ignored)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    covered_lines: int
    num_statements: int


class CoverageFile(BaseModel):
    """One file entry in coverage.json."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    summary: FileSummary


class CoverageReport(BaseModel):
    """The subset of coverage.json the gate consumes."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    files: dict[str, CoverageFile]


@dataclass(frozen=True, slots=True)
class PackageCoverage:
    """Measured line coverage for one gated package."""

    name: str
    covered_lines: int
    num_statements: int
    threshold: float

    @property
    def percent(self) -> float:
        """Line coverage as a percentage (num_statements > 0 by construction)."""
        return 100.0 * self.covered_lines / self.num_statements

    @property
    def passes(self) -> bool:
        """Whether the package meets its threshold."""
        return self.percent >= self.threshold


def run_test_suite_with_coverage() -> None:
    """Run the full suite once, writing a fresh coverage.json.

    Any stale report is removed first so the gate can never pass on old
    numbers, and ``--cov=apps`` is passed explicitly so coverage is measured
    even if the pytest addopts drift. Fails the gate if the suite fails.
    """
    COVERAGE_JSON.unlink(missing_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--cov=apps",
            "--cov-report=json:coverage.json",
            "-q",
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        msg = f"test suite failed (pytest exit code {result.returncode}); coverage not measured"
        raise CoverageGateError(msg)


def load_coverage_report(path: Path) -> CoverageReport:
    """Parse coverage.json into a typed report, or raise CoverageGateError."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read {path.name} (did the test suite run?): {exc}"
        raise CoverageGateError(msg) from exc
    if not raw.strip():
        msg = f"{path.name} is empty; cannot measure coverage"
        raise CoverageGateError(msg)
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON in {path.name}: {exc}"
        raise CoverageGateError(msg) from exc
    try:
        return CoverageReport.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(f"{error['loc']}: {error['msg']}" for error in exc.errors())
        msg = f"unexpected {path.name} structure: {details}"
        raise CoverageGateError(msg) from exc


def measure_package(report: CoverageReport, gated: PackageThreshold) -> PackageCoverage:
    """Sum statement counts over one package's src/ tree (never blended)."""
    covered = 0
    statements = 0
    for path, file_entry in report.files.items():
        if path.startswith(gated.src_prefix):
            covered += file_entry.summary.covered_lines
            statements += file_entry.summary.num_statements
    if statements == 0:
        msg = f"no coverage data for {gated.name} under {gated.src_prefix!r}; cannot measure"
        raise CoverageGateError(msg)
    return PackageCoverage(
        name=gated.name,
        covered_lines=covered,
        num_statements=statements,
        threshold=gated.min_line_coverage,
    )


def main() -> None:
    """Measure per-package coverage and fail if any package is below its floor."""
    run_test_suite_with_coverage()
    report = load_coverage_report(COVERAGE_JSON)
    results = [measure_package(report, gated) for gated in THRESHOLDS]

    print("per-package line coverage (covered/statements, from a fresh test run):")
    for result in results:
        verdict = "PASS" if result.passes else "FAIL"
        print(
            f"  {result.name:<13} {result.percent:6.2f}% "
            f"({result.covered_lines}/{result.num_statements} lines, "
            f"threshold {result.threshold:.0f}%) {verdict}"
        )

    failures = [result for result in results if not result.passes]
    if failures:
        for failed in failures:
            print(
                f"COVERAGE GATE FAILED: {failed.name} coverage {failed.percent:.2f}% "
                f"below threshold {failed.threshold:.0f}%",
                file=sys.stderr,
            )
        raise SystemExit(1)
    print("COVERAGE GATE PASSED: model-server >=80%, gateway >=70%")


if __name__ == "__main__":
    try:
        main()
    except CoverageGateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
