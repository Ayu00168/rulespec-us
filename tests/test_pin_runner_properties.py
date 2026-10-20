"""Property-based tests for tools/pin_runner.py's outcome classifier.

Invariants, for every oracle record:

1. Pass iff a reference predicate holds: `classify` passes a pin exactly when
   the runner is not distributed by xdist, the pinned file is in the checkout,
   pytest collected at least one case of the pinned function, none was
   deselected, and every case reported setup, call and teardown as `passed`
   with no xfail on the call. The predicate below is written independently of
   `case_outcome` and `worst` (a differential test of the classifier).
2. Fail closed: every pin gets an outcome from OUTCOMES, and a non-passing pin
   always carries a detail.
3. Commit level: `commit_passed` holds exactly when every pin passed, there is
   at least one pin, and the commit has no issue; a non-zero pytest exit with
   every pin passing is a commit issue.
4. `worst` returns `passed` only when every outcome is `passed`, never an
   outcome outside its inputs other than `error`, and `case_outcome` never
   raises on any JSON-shaped record.
5. The cache key ignores pin order and repeats and changes with every
   component.
6. `summarize` prints, from any report, only text that fixed patterns allow:
   every backticked span is an entry id, a commit reference or a pin id, and
   no line carries free text from the run.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sc = _load("source_conflicts", REPO_ROOT / "tools" / "source_conflicts.py")
runner = _load("pin_runner", REPO_ROOT / "tools" / "pin_runner.py")

SETTINGS = settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
COMMIT = "c" * 40
PATHS = ["tests/test_a.py", "tests/test_b.py"]
NAMES = ["test_x", "test_y", "TestK::test_x"]

outcome_values = st.sampled_from(["passed", "failed", "skipped", "rerun", None])
phase = st.fixed_dictionaries(
    {"outcome": outcome_values},
    optional={"xfail": st.booleans(), "message": st.text(max_size=5)},
)
PASSING = {when: {"outcome": "passed", "xfail": False} for when in ("setup", "call", "teardown")}
# A third of the records pass outright, so passing pins are common enough for
# the differential to compare both sides.
phase_record = st.one_of(
    st.just(PASSING),
    st.builds(lambda when, report: {**PASSING, when: report}, st.sampled_from(sorted(PASSING)), phase),
    st.dictionaries(st.sampled_from(["setup", "call", "teardown"]), st.one_of(phase, st.just("passed")), max_size=3),
)
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=5),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=8), children, max_size=3),
    max_leaves=10,
)


@st.composite
def records(draw):
    """(oracle record, pins, missing files)."""

    pins = [
        runner.Pin("us:x", f"entries[0].tests[{index}]", "PolicyEngine/a", COMMIT, path, name)
        for index, (path, name) in enumerate(
            draw(st.lists(st.tuples(st.sampled_from(PATHS), st.sampled_from(NAMES)), min_size=1, max_size=4, unique=True))
        )
    ]
    items, reports = [], {}
    for path in PATHS:
        for name in NAMES:
            for case in range(draw(st.integers(0, 2))):
                nodeid = f"{path}::{name}[{case}]"
                items.append({"nodeid": nodeid, "path": path, "name": name})
                if draw(st.booleans()) or draw(st.booleans()):
                    reports[nodeid] = draw(phase_record)
    nodeids = [item["nodeid"] for item in items]
    record = {
        "collected": items,
        "deselected": draw(st.lists(st.sampled_from(nodeids), max_size=2)) if nodeids else [],
        "collect_errors": [{"path": path} for path in draw(st.lists(st.sampled_from(PATHS), max_size=1))],
        "collect_skips": [{"path": path} for path in draw(st.lists(st.sampled_from(PATHS), max_size=1))],
        "reports": reports,
        "exitstatus": draw(st.sampled_from([0, 0, 1, 2, 5, None])),
        "xdist": draw(st.booleans()) and draw(st.booleans()),
    }
    missing = draw(st.lists(st.sampled_from(PATHS), max_size=1))
    return record, pins, missing


def reference_passes(record, pin, missing) -> bool:
    """The specification of `passed`, written without the classifier's helpers."""

    if record["xdist"] or pin.path in missing:
        return False
    items = [item for item in record["collected"] if (item["path"], item["name"]) == (pin.path, pin.name)]
    if not items:
        return False
    for item in items:
        if item["nodeid"] in record["deselected"]:
            return False
        phases = record["reports"].get(item["nodeid"]) or {}
        for when in ("setup", "call", "teardown"):
            report = phases.get(when)
            if not isinstance(report, dict) or report.get("outcome") != "passed":
                return False
        if phases["call"].get("xfail"):
            return False
    return True


# 1 and 2. Pass iff the reference predicate; fail closed otherwise.
@SETTINGS
@given(data=records())
def test_classify_passes_exactly_when_the_specification_does(data):
    record, pins, missing = data
    results, _ = runner.classify(record, pins, missing_files=missing)
    assert len(results) == len(pins)
    for result, pin in zip(results, pins, strict=True):
        assert result["id"] == pin.id
        assert result["outcome"] in runner.OUTCOMES
        assert (result["outcome"] == runner.PASSED) == reference_passes(record, pin, missing), (result, pin)
        if result["outcome"] != runner.PASSED:
            assert result.get("detail")


# 3. Commit level.
@SETTINGS
@given(data=records())
def test_commit_level(data):
    record, pins, missing = data
    results, issues = runner.classify(record, pins, missing_files=missing)
    every_pin_passed = all(result["outcome"] == runner.PASSED for result in results)
    if every_pin_passed and record["exitstatus"] != 0:
        assert any("pytest exited" in issue for issue in issues)
    passed = runner.commit_passed({"pins": results, "issues": issues})
    assert passed == (every_pin_passed and not issues and bool(results))
    if passed:
        assert record["exitstatus"] == 0 and not record["xdist"]


# 4. worst and case_outcome.
@SETTINGS
@given(outcomes=st.lists(st.sampled_from([*runner.CASE_SEVERITY, "unknown"]), max_size=5))
def test_worst(outcomes):
    result = runner.worst(outcomes)
    assert result in set(outcomes) | {"error"}
    assert (result == runner.PASSED) == (bool(outcomes) and all(outcome == runner.PASSED for outcome in outcomes))


@SETTINGS
@given(record=st.one_of(json_values, phase_record))
def test_case_outcome_is_total(record):
    assert runner.case_outcome(record if isinstance(record, dict) or record is None else {"call": record}) in runner.CASE_SEVERITY


# 5. Cache keys.
@SETTINGS
@given(
    names=st.lists(st.sampled_from(NAMES), min_size=1, max_size=4),
    other=st.sampled_from(["repo", "commit", "ids", "version"]),
    order=st.randoms(use_true_random=False),
)
def test_cache_key(names, other, order):
    pins = [runner.Pin("us:x", "l", "PolicyEngine/a", COMMIT, "tests/test_a.py", name) for name in names]
    shuffled = list(pins) + pins[:1]
    order.shuffle(shuffled)
    key = runner.cache_key("PolicyEngine/a", COMMIT, pins, "v1")
    assert key == runner.cache_key("PolicyEngine/a", COMMIT, shuffled, "v1")
    changed = {
        "repo": lambda: runner.cache_key("PolicyEngine/b", COMMIT, pins, "v1"),
        "commit": lambda: runner.cache_key("PolicyEngine/a", "d" * 40, pins, "v1"),
        "ids": lambda: runner.cache_key("PolicyEngine/a", COMMIT, [*pins, runner.Pin("us:x", "l", "PolicyEngine/a", COMMIT, "tests/test_z.py", "test_z")], "v1"),
        "version": lambda: runner.cache_key("PolicyEngine/a", COMMIT, pins, "v2"),
    }[other]()
    assert changed != key


# 6. The alarm summary prints no free text from the run.
BACKTICKED = re.compile(r"`([^`]*)`")
SAFE_SPANS = (
    re.compile(r"[a-z0-9-]+:[a-z0-9-]+"),
    re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+@[0-9a-f]{12}(:[A-Za-z0-9_./-]+::[A-Za-z_][A-Za-z0-9_]*(::[A-Za-z_][A-Za-z0-9_]*)?)?"),
    re.compile(r"pinned-tests"),
)
FIXED_TEXT = re.compile(
    r"- (`[^`]*` entries\[[0-9]+\]\.tests\[[0-9]+\]: (`[^`]*` [a-z ]+|the runner can't execute this pin\.)"
    r"|`[^`]*`: the run of this commit reported an issue\."
    r"|[0-9]+ malformed report item\(s\); the `pinned-tests` job's log has the details\."
    r"|The runner reported [0-9]+ issue\(s\) outside any pin; see the `pinned-tests` job's log\."
    r"|\.\.\. and [0-9]+ more; see the report artifact\.)"
    r"|No pinned test failed\."
)
text = st.one_of(st.text(max_size=20), st.sampled_from(["us:x", "PolicyEngine/a", COMMIT, "tests/test_a.py::test_x", "skipped", "entries[0].tests[1]"]))
pin_dicts = st.fixed_dictionaries({"entry": text, "label": text, "id": text, "outcome": st.one_of(text, st.sampled_from(sorted(runner.OUTCOMES)))})
reports = st.fixed_dictionaries({
    "commits": st.lists(st.fixed_dictionaries({"repo": text, "commit": text, "issues": st.lists(text, max_size=1), "pins": st.lists(pin_dicts, max_size=3)}), max_size=3),
    "unrun": st.lists(st.fixed_dictionaries({"entry": text, "label": text, "reason": text}), max_size=2),
    "issues": st.lists(text, max_size=2),
})


@SETTINGS
@given(report=st.one_of(reports, json_values))
def test_summarize_prints_only_fixed_text(tmp_path_factory, report):
    path = tmp_path_factory.mktemp("summary") / "report.json"
    path.write_text(json.dumps(report))
    for line in runner.summarize(path):
        assert "\n" not in line
        if line.startswith("No readable") or line.startswith("The pinned-test report is not"):
            continue
        assert FIXED_TEXT.fullmatch(line), line
        for span in BACKTICKED.findall(line):
            assert any(pattern.fullmatch(span) for pattern in SAFE_SPANS), (span, line)
