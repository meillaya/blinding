"""Tests for the eval suite loader, check scoring, and the Wilson summary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from blinding import evals
from blinding.errors import ConfigError
from blinding.stats import wilson_interval

SUITE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "core.jsonl"


def _write_raw(path: Path, text: str) -> Path:
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _write_suite(path: Path, *cases: dict[str, object]) -> Path:
    return _write_raw(path, "\n".join(json.dumps(case) for case in cases))


def _check(
    *,
    kind: evals.EvalKind = "exact",
    value: str | dict[str, object] = "yes",
) -> evals.Check:
    return evals.Check(kind=kind, value=value)


def _cases(result: dict[str, object]) -> list[evals.CaseResult]:
    return cast(list[evals.CaseResult], result["cases"])


def test_load_suite_parses_every_check_kind(tmp_path: Path) -> None:
    suite = _write_suite(
        tmp_path / "suite.jsonl",
        {
            "id": "exact",
            "prompt": "say yes",
            "check": {"kind": "exact", "value": "yes"},
            "max_tokens": 8,
        },
        {
            "id": "regex",
            "prompt": "give digits",
            "check": {"kind": "regex", "value": "^[0-9]+$"},
        },
        {
            "id": "json",
            "prompt": "give json",
            "check": {"kind": "json", "value": {"answer": 4}},
        },
        {
            "id": "contains",
            "prompt": "mention atlas",
            "check": {"kind": "contains", "value": "atlas"},
            "max_tokens": 32,
        },
    )
    cases = evals.load_suite(suite)
    assert [case.id for case in cases] == ["exact", "regex", "json", "contains"]
    assert cases[0].max_tokens == 8
    assert cases[1].max_tokens == evals.DEFAULT_MAX_TOKENS
    assert cases[2].check.kind == "json"
    assert cases[2].check.value == {"answer": 4}
    assert cases[3].check.value == "atlas"


def test_load_suite_accepts_type_as_a_kind_alias(tmp_path: Path) -> None:
    line = json.dumps(
        {
            "id": "old-style",
            "prompt": "say yes",
            "check": {"type": "exact", "value": "yes"},
        }
    )
    cases = evals.load_suite(_write_raw(tmp_path / "suite.jsonl", line))
    assert cases[0].check.kind == "exact"


BAD_LINES = [
    "not json",
    "[]",
    json.dumps({"prompt": "p", "check": {"kind": "exact", "value": "yes"}, "max_tokens": 8}),
    json.dumps({"id": "", "prompt": "p", "check": {"kind": "exact", "value": "yes"}}),
    json.dumps({"id": "a", "prompt": "", "check": {"kind": "exact", "value": "yes"}}),
    json.dumps({"id": "a", "prompt": "p", "check": {"kind": "exact"}}),
    json.dumps({"id": "a", "prompt": "p", "check": {"kind": "wat", "value": "x"}}),
    json.dumps({"id": "a", "prompt": "p", "check": {"kind": "json", "value": "x"}}),
    json.dumps({"id": "a", "prompt": "p", "check": {"kind": "regex", "value": "("}}),
    json.dumps(
        {"id": "a", "prompt": "p", "check": {"kind": "exact", "value": "x"}, "max_tokens": 0}
    ),
    json.dumps(
        {"id": "a", "prompt": "p", "check": {"kind": "exact", "value": "x"}, "max_tokens": "8"}
    ),
    json.dumps({"id": "a", "prompt": "p", "check": {"kind": "exact", "value": "x"}, "extra": 1}),
    json.dumps(
        {
            "id": "a",
            "prompt": "p",
            "check": {"kind": "exact", "type": "exact", "value": "x"},
        }
    ),
    json.dumps({"id": "a", "prompt": "p", "check": {"value": "x"}}),
]


@pytest.mark.parametrize("line", BAD_LINES)
def test_load_suite_rejects_a_bad_line(tmp_path: Path, line: str) -> None:
    suite = _write_raw(tmp_path / "bad.jsonl", line)
    with pytest.raises(ConfigError):
        evals.load_suite(suite)


def test_load_suite_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = json.dumps({"id": "same", "prompt": "p", "check": {"kind": "exact", "value": "x"}})
    suite = _write_raw(tmp_path / "duplicate.jsonl", line + "\n" + line)
    with pytest.raises(ConfigError, match="duplicate"):
        evals.load_suite(suite)


def test_load_suite_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        evals.load_suite(tmp_path / "missing.jsonl")


def test_core_prompt_suite_is_valid() -> None:
    cases = evals.load_suite(SUITE_PATH)
    assert len(cases) == 16
    assert len({case.id for case in cases}) == 16
    assert {case.check.kind for case in cases} == {
        "exact",
        "regex",
        "json",
        "contains",
    }


def test_score_exact_ignores_surrounding_whitespace() -> None:
    check = _check(value="BLUE HERON")
    assert evals.score(check, "  BLUE HERON\n") is True
    assert evals.score(check, "blue heron") is False


def test_score_contains_is_case_sensitive() -> None:
    check = _check(kind="contains", value="atlas")
    assert evals.score(check, "A map called an atlas.") is True
    assert evals.score(check, "ATLAS") is False


def test_score_regex_searches_anywhere() -> None:
    check = _check(kind="regex", value=r"\b[0-9]{2}\b")
    assert evals.score(check, "answer: 42") is True
    assert evals.score(check, "answer: 4") is False


def test_score_json_checks_fixed_keys() -> None:
    check = _check(kind="json", value={"sum": 42, "unit": "days"})
    assert evals.score(check, '{"sum": 42, "unit": "days"}') is True
    assert evals.score(check, '{"sum": 41, "unit": "days"}') is False
    assert evals.score(check, '{"sum": 42}') is False
    assert evals.score(check, "42") is False
    assert evals.score(check, "not json") is False


def test_score_json_with_an_empty_object_is_parse_only() -> None:
    check = _check(kind="json", value={})
    assert evals.score(check, '{"anything": [1, 2]}') is True
    assert evals.score(check, "not json") is False


def test_run_eval_scores_with_a_fake_chat() -> None:
    calls: list[tuple[str, int]] = []

    async def fake_chat(prompt: str, max_tokens: int) -> tuple[bool, str, str]:
        calls.append((prompt, max_tokens))
        if prompt == "say yes":
            return (True, " yes \n", "")
        if prompt == "say two":
            return (True, "3", "")
        return (False, "", "HTTP 500")

    cases = [
        evals.EvalCase(
            id="yes",
            prompt="say yes",
            check=_check(value="yes"),
            max_tokens=8,
        ),
        evals.EvalCase(
            id="two",
            prompt="say two",
            check=_check(value="2"),
        ),
        evals.EvalCase(
            id="error",
            prompt="explode",
            check=_check(kind="contains", value="x"),
        ),
    ]
    result = asyncio.run(evals.run_eval(cases, fake_chat))
    cases_out = _cases(result)
    assert calls == [("say yes", 8), ("say two", 64), ("explode", 64)]
    assert [case["ok"] for case in cases_out] == [True, False, False]
    assert cases_out[2]["error"] == "HTTP 500"
    assert result["passed"] == 1
    assert result["trials"] == 3
    assert result["rate"] == pytest.approx(1 / 3)
    low = cast(float, result["wilson_low"])
    rate = cast(float, result["rate"])
    high = cast(float, result["wilson_high"])
    assert 0.0 <= low < rate < high <= 1.0


def test_run_eval_records_a_chat_exception() -> None:
    async def broken_chat(prompt: str, max_tokens: int) -> tuple[bool, str, str]:
        raise RuntimeError("connection reset")

    cases = [evals.EvalCase(id="boom", prompt="p", check=_check(value="x"))]
    result = asyncio.run(evals.run_eval(cases, broken_chat))
    cases_out = _cases(result)
    assert result["passed"] == 0
    assert cases_out[0]["ok"] is False
    assert "connection reset" in cases_out[0]["error"]


def test_run_eval_of_an_empty_suite() -> None:
    async def fake_chat(prompt: str, max_tokens: int) -> tuple[bool, str, str]:
        return (True, "", "")

    result = asyncio.run(evals.run_eval([], fake_chat))
    assert _cases(result) == []
    assert result["trials"] == 0
    assert cast(float, result["rate"]) == 0.0
    assert cast(float, result["wilson_low"]) == 0.0
    assert cast(float, result["wilson_high"]) == 0.0


def test_wilson_interval_stays_inside_the_unit_interval() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)
    low, high = wilson_interval(8, 10)
    assert 0.0 < low < 0.8 < high < 1.0
    low_zero, high_zero = wilson_interval(0, 10)
    assert low_zero == 0.0
    assert 0.0 < high_zero < 0.5
    low_all, high_all = wilson_interval(10, 10)
    assert high_all == 1.0
    assert 0.5 < low_all < 1.0


def test_summary_rows_truncate_and_collapse_answers() -> None:
    async def fake_chat(prompt: str, max_tokens: int) -> tuple[bool, str, str]:
        if prompt == "long":
            return (True, "first line\nsecond line " + "x" * 100, "")
        return (True, "85", "")

    cases = [
        evals.EvalCase(
            id="long",
            prompt="long",
            check=_check(kind="contains", value="second line"),
        ),
        evals.EvalCase(id="short", prompt="short", check=_check(value="85")),
    ]
    result = asyncio.run(evals.run_eval(cases, fake_chat))
    rows = evals.summary_rows(result)
    assert rows[0][0] == "long"
    assert rows[0][1] == "pass"
    assert "\n" not in rows[0][2]
    assert len(rows[0][2]) == evals.SNIPPET_CHARS
    assert rows[1] == ["short", "pass", "85"]
