"""Deterministic eval suite for a small instruct model.

The suite is JSONL so a prompt and its expected form stay on one line, and a
failed case can be copied into a bug report unchanged. Scoring is mechanical:
an answer passes or it does not. The run reports the pass count with a Wilson
interval because sixteen prompts are a small sample, and a bare percentage
hides how wide that sample is.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from blinding.errors import ConfigError
from blinding.stats import wilson_interval

DEFAULT_MAX_TOKENS = 64
SNIPPET_CHARS = 60
EvalKind = Literal["exact", "regex", "json", "contains"]
CHECK_KINDS: frozenset[str] = frozenset({"exact", "regex", "json", "contains"})
CASE_FIELDS = frozenset({"id", "prompt", "check", "max_tokens"})
# "type" is accepted as an alias for "kind" because early suite files used it;
# a line that carries both is a mistake and is rejected.
CHECK_FIELDS = frozenset({"kind", "type", "value"})


@dataclass(frozen=True, slots=True)
class Check:
    """One acceptance rule for a model answer."""

    kind: EvalKind
    value: str | dict[str, object]

    def __post_init__(self) -> None:
        if self.kind not in CHECK_KINDS:
            raise ConfigError(f"unknown check kind {self.kind!r}")
        if self.kind == "json":
            if not isinstance(self.value, dict):
                raise ConfigError("json check value must be an object")
            for key in self.value:
                if not isinstance(key, str) or not key:
                    raise ConfigError("json check keys must be non-empty strings")
            return
        if not isinstance(self.value, str) or not self.value:
            raise ConfigError(f"{self.kind} check value must be a non-empty string")
        if self.kind == "regex":
            try:
                re.compile(self.value)
            except re.error as exc:
                raise ConfigError(f"invalid regex {self.value!r}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One prompt and the check its answer must pass."""

    id: str
    prompt: str
    check: Check
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ConfigError("eval case id must be a non-empty string")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ConfigError(f"{self.id}: prompt must be a non-empty string")
        if not isinstance(self.check, Check):
            raise ConfigError(f"{self.id}: check must be a Check instance")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise ConfigError(f"{self.id}: max_tokens must be an integer")
        if self.max_tokens <= 0:
            raise ConfigError(f"{self.id}: max_tokens must be positive")


def _check_kind(raw: Mapping[str, object], where: str) -> str:
    """Kind name from a suite row, which may spell the key kind or type."""
    names = [key for key in ("kind", "type") if key in raw]
    if len(names) != 1:
        raise ConfigError(f"{where}: check needs exactly one of kind or type")
    value = raw[names[0]]
    if not isinstance(value, str) or value not in CHECK_KINDS:
        raise ConfigError(f"{where}: unknown check kind {value!r}")
    return value


def _check_value(raw: object, where: str) -> str | dict[str, object]:
    """Normalize a check value; a JSON check may carry an object of pairs."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    raise ConfigError(f"{where}: check value must be a string or an object")


def load_suite(path: Path) -> list[EvalCase]:
    """Parse and validate a JSONL suite before any request is sent."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read eval suite {path}: {exc}") from exc

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        where = f"{path}:{number}"
        try:
            parsed: object = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{where}: invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError(f"{where}: each line must be a JSON object")
        case_data: dict[str, object] = {str(key): value for key, value in parsed.items()}
        unknown = sorted(set(case_data) - CASE_FIELDS)
        if unknown:
            raise ConfigError(f"{where}: unknown case fields: {', '.join(unknown)}")

        raw_check = case_data.get("check")
        if not isinstance(raw_check, dict):
            raise ConfigError(f"{where}: check must be an object")
        check_data: dict[str, object] = {str(key): value for key, value in raw_check.items()}
        unknown_check = sorted(set(check_data) - CHECK_FIELDS)
        if unknown_check:
            raise ConfigError(f"{where}: unknown check fields: {', '.join(unknown_check)}")
        kind = _check_kind(check_data, where)
        if "value" in check_data:
            raw_value = check_data["value"]
        elif kind == "json":
            raw_value = {}
        else:
            raise ConfigError(f"{where}: check needs a value")
        try:
            check = Check(
                kind=cast(EvalKind, kind),
                value=_check_value(raw_value, where),
            )
        except ConfigError as exc:
            raise ConfigError(f"{where}: {exc}") from exc

        case_id = case_data.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ConfigError(f"{where}: id must be a non-empty string")
        prompt = case_data.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ConfigError(f"{where}: prompt must be a non-empty string")
        raw_max = case_data.get("max_tokens", DEFAULT_MAX_TOKENS)
        if isinstance(raw_max, bool) or not isinstance(raw_max, int):
            raise ConfigError(f"{where}: max_tokens must be an integer")
        try:
            case = EvalCase(id=case_id, prompt=prompt, check=check, max_tokens=raw_max)
        except ConfigError as exc:
            raise ConfigError(f"{where}: {exc}") from exc
        if case.id in seen:
            raise ConfigError(f"{where}: duplicate id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return cases


def _text_value(check: Check) -> str:
    if not isinstance(check.value, str):
        raise ConfigError(f"{check.kind} check does not hold a string")
    return check.value


def _score_json(expected: dict[str, object], text: str) -> bool:
    try:
        parsed: object = json.loads(text.strip())
    except json.JSONDecodeError:
        return False
    if not expected:
        return True
    if not isinstance(parsed, dict):
        return False
    return all(key in parsed and parsed[key] == value for key, value in expected.items())


def score(check: Check, text: str) -> bool:
    """Whether `text` passes `check`; every rule is mechanical on purpose."""
    if check.kind == "exact":
        return text.strip() == _text_value(check).strip()
    if check.kind == "contains":
        return _text_value(check) in text
    if check.kind == "regex":
        return re.search(_text_value(check), text) is not None
    value = check.value
    if not isinstance(value, dict):
        raise ConfigError("json check does not hold an object")
    return _score_json(value, text)


class CaseResult(TypedDict):
    """One case after its answer was requested and scored."""

    id: str
    ok: bool
    text: str
    error: str


async def run_eval(
    cases: Sequence[EvalCase],
    chat: Callable[[str, int], Awaitable[tuple[bool, str, str]]],
) -> dict[str, object]:
    """Run every case once and score the answers.

    Requests are issued one at a time: a format probe wants the model's answer,
    not a queueing measurement, and a small model server gains nothing from
    concurrent cases. One failing request is recorded and the suite continues,
    because the pass rate over the remaining prompts is still evidence.
    """
    results: list[CaseResult] = []
    passed = 0
    for case in cases:
        try:
            ok, text, error = await chat(case.prompt, case.max_tokens)
        except Exception as exc:  # a transport bug must not hide the other cases
            ok, text, error = False, "", f"{type(exc).__name__}: {exc}"
        case_ok = ok and score(case.check, text)
        if case_ok:
            passed += 1
        results.append(CaseResult(id=case.id, ok=case_ok, text=text, error=error))
    trials = len(cases)
    low, high = wilson_interval(passed, trials)
    record: dict[str, object] = {
        "cases": results,
        "passed": passed,
        "trials": trials,
        "rate": passed / trials if trials else 0.0,
        "wilson_low": low,
        "wilson_high": high,
    }
    return record


def summary_rows(result: Mapping[str, object]) -> list[list[str]]:
    """Table rows: case id, verdict, and a one line answer snippet."""
    cases = cast(list[CaseResult], result.get("cases", []))
    rows: list[list[str]] = []
    for case in cases:
        snippet = " ".join(case["text"].split())[:SNIPPET_CHARS]
        rows.append([case["id"], "pass" if case["ok"] else "fail", snippet])
    return rows
