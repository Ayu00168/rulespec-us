"""Property-based tests for tools/pin_runner.py's outcome classifier.

Invariants, for every oracle record:

1. Pass iff a reference predicate holds (Python tests and YAML cases, whose
   node ids repeat, read by collection index): `classify` passes a pin exactly when
   the runner is not distributed by xdist, the pinned file is in the checkout,
   pytest collected at least one case of the pinned function, none was
   deselected, and every case reported setup, call and teardown as `passed`
   with no xfail on the call. The predicate below is written independently of
   `case_outcome` and `worst` (a differential test of the classifier).
2. Fail closed: every pin gets an outcome from OUTCOMES, and a non-passing pin
   always carries a detail.
3. Commit level: `commit_passed` holds exactly when there is at least one
   pin, every pin passed, pytest's process exited 0 and the session's own
   status was 0. The process exit and the session status are separate
   conditions, and each alone fails the commit.
4. `worst` returns `passed` only when every outcome is `passed`, never an
   outcome outside its inputs other than `error`, and `case_outcome` never
   raises on any JSON-shaped record.
5. The cache key ignores pin order and repeats and changes with every
   component.
6. `summarize` prints, from any report, only text that fixed patterns allow:
   every backticked span is an entry id, a commit reference or a pin id, and
   no line carries free text from the run. For any JSON artifact, any field
   of any type, `--summarize` exits 0 and prints either a valid summary or
   the malformed-artifact alarm.
7. Provenance: for any answers GitHub's API could give, `commit_provenance`
   never raises, and says a commit is shipped exactly when the canonical name
   is the pinned one, the repository says it is not a fork, and compare says
   `identical` or `behind`. Any exception from the API is a refusal.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sys
import urllib.error
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
YAML_PATH = "policyengine_us/tests/cases.yaml"
NAMES = ["test_x", "test_y", "TestK::test_x"]
YAML_NAMES = ["A case (2024)", "Another case"]
PIN_TARGETS = [(path, name) for path in PATHS for name in NAMES] + [(YAML_PATH, name) for name in YAML_NAMES]

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
            draw(st.lists(st.sampled_from(PIN_TARGETS), min_size=1, max_size=4, unique=True))
        )
    ]
    items, reports = [], {}
    for path in PATHS + [YAML_PATH]:
        for name in YAML_NAMES if path == YAML_PATH else NAMES:
            for case in range(draw(st.integers(0, 2))):
                # A YAML runner gives every case in a file the same node id,
                # so only the collection index tells cases apart.
                nodeid = f"{path}::" if path == YAML_PATH else f"{path}::{name}[{case}]"
                index = len(items)
                items.append({"index": index, "nodeid": nodeid, "path": path, "name": name})
                if draw(st.booleans()) or draw(st.booleans()):
                    reports[str(index)] = draw(phase_record)
    indexes = [item["index"] for item in items]
    record = {
        "collected": items,
        "deselected": draw(st.lists(st.sampled_from(indexes), max_size=2)) if indexes else [],
        "collect_errors": [{"path": path} for path in draw(st.lists(st.sampled_from(PATHS), max_size=1))],
        "collect_skips": [{"path": path} for path in draw(st.lists(st.sampled_from(PATHS), max_size=1))],
        "reports": reports,
        "exitstatus": draw(st.sampled_from([0, 0, 1, 2, 5, None])),
        "xdist": draw(st.booleans()) and draw(st.booleans()),
    }
    missing = draw(st.lists(st.sampled_from(PATHS), max_size=1))
    process_exit = draw(st.sampled_from([0, 0, 0, 1, 2, 4, None]))
    return record, pins, missing, process_exit


def reference_passes(record, pin, missing) -> bool:
    """The specification of `passed`, written without the classifier's helpers."""

    if record["xdist"] or pin.path in missing:
        return False
    items = [item for item in record["collected"] if (item["path"], item["name"]) == (pin.path, pin.name)]
    if not items:
        return False
    for item in items:
        if item["index"] in record["deselected"]:
            return False
        phases = record["reports"].get(str(item["index"])) or {}
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
    record, pins, missing, process_exit = data
    results, _ = runner.classify(record, pins, process_exit=process_exit, missing_files=missing)
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
    record, pins, missing, process_exit = data
    results, issues = runner.classify(record, pins, process_exit=process_exit, missing_files=missing)
    every_pin_passed = all(reference_passes(record, pin, missing) for pin in pins)
    passed = runner.commit_passed({"pins": results, "issues": issues, "pytest_exit": process_exit})
    # Specification: the pins, the process exit and the session status, each
    # required on its own.
    assert passed == (every_pin_passed and process_exit == 0 and record["exitstatus"] == 0 and bool(pins))
    if process_exit not in (0, None):
        assert f"pytest's process exited {process_exit}" in issues
    if process_exit is not None and record["exitstatus"] != 0:
        assert f"pytest's session reported exit status {record['exitstatus']}" in issues


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
    # A YAML case id: a free-text case name, but no backtick, @, angle
    # bracket or line break.
    re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+@[0-9a-f]{12}:[A-Za-z0-9_./-]+\.ya?ml::[A-Za-z0-9][A-Za-z0-9 ,.;:()'%$&+/_#-]*"),
)
FIXED_TEXT = re.compile(
    r"- (`[^`]*` entries\[[0-9]+\]\.tests\[[0-9]+\]: (`[^`]*` [a-z ]+|the runner can't execute this pin\.)"
    r"|`[^`]*`: the run of this commit reported an issue\."
    r"|pinned-tests artifact malformed( \([0-9]+ item\(s\)\))?; the `pinned-tests` job's log has the details\."
    r"|pinned-tests artifact missing or unreadable; the `pinned-tests` job's log has the details\."
    r"|The runner reported [0-9]+ issue\(s\) outside any pin; see the `pinned-tests` job's log\."
    r"|\.\.\. and [0-9]+ more; see the report artifact\.)"
    r"|No pinned test failed\."
)
# Any JSON value may sit where a string belongs (round-8 review: `outcome: []`
# made the summarizer raise).
text = st.one_of(
    st.text(max_size=20),
    st.sampled_from(["us:x", "PolicyEngine/a", COMMIT, "tests/test_a.py::test_x", "skipped", "entries[0].tests[1]",
                     "policyengine_us/tests/cases.yaml::A case (2024)", "policyengine_us/tests/cases.yaml::x `@team`"]),
    json_values,
)
pin_dicts = st.fixed_dictionaries({"entry": text, "label": text, "id": text, "outcome": st.one_of(text, st.sampled_from(sorted(runner.OUTCOMES)))})
reports = st.fixed_dictionaries({
    "commits": st.one_of(
        st.lists(st.one_of(
            st.fixed_dictionaries({"repo": text, "commit": text, "issues": st.one_of(st.lists(text, max_size=1), text),
                                   "pins": st.one_of(st.lists(st.one_of(pin_dicts, json_values), max_size=3), text)}),
            json_values,
        ), max_size=3),
        json_values,
    ),
    "unrun": st.one_of(st.lists(st.fixed_dictionaries({"entry": text, "label": text, "reason": text}), max_size=2), json_values),
    "issues": st.one_of(st.lists(text, max_size=2), json_values),
})


@SETTINGS
@given(report=st.one_of(reports, json_values))
def test_summarize_prints_only_fixed_text(tmp_path_factory, report):
    path = tmp_path_factory.mktemp("summary") / "report.json"
    path.write_text(json.dumps(report))
    for line in runner.summarize(path):
        assert "\n" not in line
        assert FIXED_TEXT.fullmatch(line), line
        for span in BACKTICKED.findall(line):
            assert any(pattern.fullmatch(span) for pattern in SAFE_SPANS), (span, line)


@SETTINGS
@given(report=st.one_of(reports, json_values))
def test_summarize_cli_exits_zero_on_any_artifact(tmp_path_factory, report):
    path = tmp_path_factory.mktemp("cli") / "report.json"
    path.write_text(json.dumps(report))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert runner.main(["--summarize", str(path)]) == 0
    lines = out.getvalue().splitlines()
    assert lines and all(FIXED_TEXT.fullmatch(line) for line in lines), lines


# 7. Provenance.
api_answers = st.one_of(
    json_values,
    st.fixed_dictionaries(
        {},
        optional={
            "full_name": st.sampled_from(["PolicyEngine/a", "policyengine/a", "Other/a", None, 7]),
            "fork": st.sampled_from([False, True, None, 0, "false"]),
            "default_branch": st.sampled_from(["main", "master", "", None, 3]),
            "status": st.sampled_from(["identical", "behind", "ahead", "diverged", None, ["behind"]]),
        },
    ),
)
api_errors = st.sampled_from([
    urllib.error.URLError("offline"), ValueError("bad json"), OSError("reset"),
    urllib.error.HTTPError("u", 404, "x", None, None), urllib.error.HTTPError("u", 301, "x", None, None),
])


@SETTINGS
@given(meta=st.one_of(api_answers, api_errors), compare=st.one_of(api_answers, api_errors))
def test_provenance_is_total_and_exact(meta, compare):
    def get(path):
        answer = meta if path == "/repos/PolicyEngine/a" else compare
        if isinstance(answer, Exception):
            raise answer
        return answer

    record = sc.commit_provenance("PolicyEngine/a", COMMIT, get)
    expected = (
        isinstance(meta, dict)
        and meta.get("full_name") == "PolicyEngine/a"
        and meta.get("fork") is False
        and isinstance(meta.get("default_branch"), str)
        and bool(meta.get("default_branch"))
        and isinstance(compare, dict)
        and compare.get("status") in ("identical", "behind")
    )
    assert record["shipped"] is expected
    assert (record["reason"] is None) is expected
