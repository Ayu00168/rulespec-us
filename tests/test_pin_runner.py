"""Tests for tools/pin_runner.py and the workflow job that runs it.

Three kinds:

- the outcome classifier and pin selection, on synthetic oracle records;
- the source-conflicts workflow's isolation of the `pinned-tests` job, read
  statically from the workflow file;
- the whole runner on fixture repositories (`git init` in a temporary
  directory, a tiny pyproject.toml), one per way a pin can fail closed. These
  need git, uv and the package index, so they run where
  AXIOM_PIN_RUNNER_TESTS_REQUIRED=1 (the pinned-tests job sets it, and there
  a missing tool fails them) and skip elsewhere, such as the Repository Checks
  leg, which should not depend on the package index.

Property-based tests of the classifier live in test_pin_runner_properties.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "source-conflicts.yml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sc = _load("source_conflicts", REPO_ROOT / "tools" / "source_conflicts.py")
runner = _load("pin_runner", REPO_ROOT / "tools" / "pin_runner.py")

COMMIT = "c" * 40
UV = shutil.which("uv")
GIT = shutil.which("git")
REQUIRED = os.environ.get("AXIOM_PIN_RUNNER_TESTS_REQUIRED") == "1"
needs_tools = pytest.mark.skipif(
    not REQUIRED,
    reason="fixture-repository tests need git, uv and the package index; the pinned-tests job runs them "
    "(AXIOM_PIN_RUNNER_TESTS_REQUIRED=1)",
)


def pin(name="test_ok", path="tests/test_main.py", repo="PolicyEngine/fixture", commit=COMMIT):
    return runner.Pin("us:fixture", "entries[0].tests[0]", repo, commit, path, name)


def phases(setup="passed", call="passed", teardown="passed", xfail=False):
    record = {}
    for when, outcome in (("setup", setup), ("call", call), ("teardown", teardown)):
        if outcome is not None:
            record[when] = {"outcome": outcome, "xfail": xfail and when == "call", "message": ""}
    return record


def oracle(items=(), *, reports=None, deselected=(), errors=(), skips=(), exitstatus=0, xdist=False):
    """A synthetic oracle record; items are (nodeid, path, name)."""

    return {
        "collected": [{"nodeid": nodeid, "path": path, "name": name} for nodeid, path, name in items],
        "deselected": list(deselected),
        "collect_errors": [{"nodeid": path, "path": path, "message": "boom"} for path in errors],
        "collect_skips": [{"nodeid": path, "path": path, "message": "skip"} for path in skips],
        "reports": reports or {},
        "exitstatus": exitstatus,
        "xdist": xdist,
    }


# ---------------------------------------------------------------------------
# Classification


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "not run"),
        ({}, "not run"),
        (phases(), "passed"),
        (phases(setup="failed", call=None, teardown="passed"), "error"),
        (phases(setup="skipped", call=None, teardown="passed"), "skipped"),
        ({"setup": {"outcome": "skipped", "xfail": True}, "teardown": {"outcome": "passed"}}, "xfail"),
        (phases(call=None), "not run"),
        (phases(call="failed"), "failed"),
        (phases(call="skipped"), "skipped"),
        (phases(call="skipped", xfail=True), "xfail"),
        (phases(xfail=True), "xpass"),
        (phases(teardown=None), "error"),
        (phases(teardown="failed"), "error"),
        (phases(teardown="skipped"), "error"),
        ({"setup": {"outcome": "passed"}, "call": {"outcome": "rerun"}, "teardown": {"outcome": "passed"}}, "error"),
        ({"setup": "passed", "call": {"outcome": "passed"}, "teardown": {"outcome": "passed"}}, "error"),
        ({"call": {"outcome": "passed"}, "teardown": {"outcome": "passed"}}, "not run"),
    ],
)
def test_case_outcome(record, expected):
    assert runner.case_outcome(record) == expected


def test_worst():
    assert runner.worst(["passed", "passed"]) == "passed"
    assert runner.worst(["passed", "skipped", "xfail"]) == "xfail"
    assert runner.worst(["not run", "passed"]) == "not run"
    assert runner.worst(["failed", "error"]) == "error"
    assert runner.worst([]) == "error"
    assert runner.worst(["passed", "something new"]) == "error"  # an unknown outcome never passes
    assert runner.worst(["something new"]) == "error"


def test_classify_passes_only_every_case_passing():
    one, two = "tests/test_main.py::test_p[1]", "tests/test_main.py::test_p[2]"
    items = [(one, "tests/test_main.py", "test_p"), (two, "tests/test_main.py", "test_p")]
    results, issues = runner.classify(oracle(items, reports={one: phases(), two: phases()}), [pin("test_p")])
    assert (results[0]["outcome"], len(results[0]["cases"]), issues) == ("passed", 2, [])
    results, issues = runner.classify(oracle(items, reports={one: phases(), two: phases(call="skipped")}, exitstatus=0), [pin("test_p")])
    assert results[0]["outcome"] == "skipped" and "1 of 2 case(s) did not pass, first " + two in results[0]["detail"]
    results, _ = runner.classify(oracle(items, reports={one: phases()}), [pin("test_p")])
    assert results[0]["outcome"] == "not run"  # a collected case that never reported


def test_classify_matches_by_path_and_name_not_node_id():
    # The node id is relative to the rootdir, which need not be the checkout root.
    item = ("sub/tests/test_main.py::TestK::test_m", "tests/test_main.py", "TestK::test_m")
    results, _ = runner.classify(oracle([item], reports={item[0]: phases()}), [pin("TestK::test_m")])
    assert results[0]["outcome"] == "passed"
    results, _ = runner.classify(oracle([item], reports={item[0]: phases()}), [pin("test_m")])
    assert results[0]["outcome"] == "missing"
    # The same function name in another file is another test.
    other = ("tests/test_other.py::TestK::test_m", "tests/test_other.py", "TestK::test_m")
    results, _ = runner.classify(oracle([other], reports={other[0]: phases()}), [pin("TestK::test_m")])
    assert results[0]["outcome"] == "missing"


@pytest.mark.parametrize(
    ("record", "missing_files", "expected"),
    [
        (oracle(), ["tests/test_main.py"], "missing file"),
        (oracle(errors=["tests/test_main.py"]), [], "collection error"),
        (oracle(skips=["tests/test_main.py"]), [], "not collected"),
        (oracle(skips=["tests/test_other.py"]), [], "missing"),
        (oracle(), [], "missing"),
        (oracle([("n", "tests/test_main.py", "test_ok")], deselected=["n"], reports={}), [], "deselected"),
        (oracle([("n", "tests/test_main.py", "test_ok")], reports={"n": phases()}, xdist=True), [], "error"),
    ],
)
def test_classify_fails_closed(record, missing_files, expected):
    results, _ = runner.classify(record, [pin()], missing_files=missing_files)
    assert results[0]["outcome"] == expected
    assert results[0]["detail"]


def test_classify_commit_issues():
    item = ("n", "tests/test_main.py", "test_ok")
    _, issues = runner.classify(oracle([item], reports={"n": phases()}, exitstatus=1), [pin()])
    assert issues == ["pytest exited 1 although every pinned case passed"]
    _, issues = runner.classify(oracle([item], reports={"n": phases()}, exitstatus=None), [pin()])
    assert issues == ["pytest exited None although every pinned case passed"]
    _, issues = runner.classify(oracle([item], reports={"n": phases()}, xdist=True), [pin()])
    assert any("pytest-xdist" in issue for issue in issues)
    _, issues = runner.classify(oracle([item], reports={"n": phases(call="failed")}, exitstatus=1), [pin()])
    assert issues == []  # the failing pin is the issue


def test_commit_passed():
    good = {"pins": [{"outcome": "passed"}], "issues": []}
    assert runner.commit_passed(good)
    assert not runner.commit_passed({**good, "issues": ["x"]})
    assert not runner.commit_passed({"pins": [], "issues": []})
    assert not runner.commit_passed({"pins": [{"outcome": "passed"}, {"outcome": "xfail"}], "issues": []})


# ---------------------------------------------------------------------------
# Which pins run, Python, install specs, cache keys


def register(*tests):
    return {"version": 1, "entries": [{"jurisdiction": "us", "id": "fixture", "tests": list(tests)}]}


def test_collect_pins_groups_runnable_pins_and_refuses_the_rest():
    good = {"repo": "PolicyEngine/a", "commit": COMMIT, "path": "tests/test_a.py", "name": "test_x", "pins": ["reading"]}
    groups, unrun = runner.collect_pins(register(
        good,
        {**good, "name": "TestK::test_y"},
        {**good, "repo": "policyengine/b"},  # owners are case-insensitive on GitHub
        {**good, "repo": "SomeoneElse/c"},
        {**good, "path": "tests/cases.yaml", "name": "a case"},
        {"path": "tests/test_local.py", "name": "test_z", "pins": ["reading"]},
        {**good, "commit": "abc"},
        {**good, "path": "tests/helpers.py"},
        "junk",
    ))
    assert sorted(groups) == [("PolicyEngine/a", COMMIT), ("policyengine/b", COMMIT)]
    assert [p.name for p in groups[("PolicyEngine/a", COMMIT)]] == ["test_x", "TestK::test_y"]
    reasons = [item["reason"] for item in unrun]
    assert len(reasons) == 6
    assert reasons[0].startswith("the runner won't execute pins in SomeoneElse/c")
    assert "YAML test case" in reasons[1]
    assert "this repository" in reasons[2]
    assert "malformed commit" in reasons[3]
    assert "not in a test_*.py file" in reasons[4]
    assert "not a mapping" in reasons[5]
    assert all(item["entry"] == "us:fixture" for item in unrun)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (None, "3.14"),
        (">=3.10,<3.15", "3.14"),
        ("<3.13", "3.12"),
        ("==3.11.*", "3.11"),
        ("~=3.10", "3.14"),
        (">=3.10.4,<3.11", "3.10"),
        ("!=3.14.*", "3.13"),
    ],
)
def test_pick_python(spec, expected):
    assert runner.pick_python(spec) == expected


@pytest.mark.parametrize("spec", [">=3.15", "<3.9", "not a specifier"])
def test_pick_python_fails_closed(spec):
    with pytest.raises(runner.StepError) as caught:
        runner.pick_python(spec)
    assert caught.value.outcome == "install failed"


def test_install_spec_table(monkeypatch):
    assert runner.install_spec("PolicyEngine/a") == ("-e", ".[dev]")
    monkeypatch.setitem(runner.INSTALL_SPECS, "PolicyEngine/policyengine-us", ("-e", ".[test]"))
    assert runner.install_spec("policyengine/PolicyEngine-US") == ("-e", ".[test]")
    assert runner.install_spec("PolicyEngine/a") == ("-e", ".[dev]")


def test_allowed_owners():
    assert runner.allowed_repo("TheAxiomFoundation/rulespec-us")
    assert runner.allowed_repo("PolicyEngine/microcosm-dynamics")
    assert not runner.allowed_repo("PolicyEngineX/fork")
    assert not runner.allowed_repo("someone/TheAxiomFoundation")


def test_cache_key():
    a, b = pin("test_a"), pin("test_b")
    key = runner.cache_key("PolicyEngine/a", COMMIT, [a, b], "v1")
    assert key == runner.cache_key("PolicyEngine/a", COMMIT, [b, a, a], "v1")
    assert len({
        key,
        runner.cache_key("PolicyEngine/b", COMMIT, [a, b], "v1"),
        runner.cache_key("PolicyEngine/a", "d" * 40, [a, b], "v1"),
        runner.cache_key("PolicyEngine/a", COMMIT, [a], "v1"),
        runner.cache_key("PolicyEngine/a", COMMIT, [a, b], "v2"),
    }) == 5


def test_the_cache_holds_and_returns_only_passing_commits(tmp_path):
    # Both sides check: _store writes only passing commits, and _cached
    # re-checks what it reads, so neither a failure nor a stale entry passes.
    settings_ = runner.Settings(uv="uv", git="git", work=tmp_path, cache_dir=tmp_path / "cache")
    pins = [pin("test_a"), pin("test_b")]
    key = runner.cache_key("PolicyEngine/fixture", COMMIT, pins, "v1")

    def result(*outcomes, issues=()):
        return {
            "repo": "PolicyEngine/fixture", "commit": COMMIT, "issues": list(issues), "logs": {"pytest": "x"},
            "pins": [{"entry": "us:old", "label": "entries[9].tests[9]", "id": p.id, "outcome": o, "cases": []}
                     for p, o in zip(pins, outcomes, strict=True)],
        }

    for failing in (result("passed", "skipped"), result("passed", "passed", issues=["pytest exited 1"])):
        runner._store(settings_, key, failing)
        assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").iterdir())
    runner._store(settings_, key, result("passed", "passed"))
    stored = json.loads((tmp_path / "cache" / f"{key}.json").read_text())
    assert stored["key"] == key and stored["logs"] == {}
    hit = runner._cached(settings_, key, pins)
    assert hit["cached"] is True
    assert [(p["entry"], p["label"]) for p in hit["pins"]] == [("us:fixture", "entries[0].tests[0]")] * 2
    # A planted entry that did not pass, belongs to another key or covers
    # other ids is ignored.
    path = tmp_path / "cache" / f"{key}.json"
    for planted in (
        {**result("passed", "failed"), "key": key},
        {**result("passed", "passed"), "key": "another"},
        {**result("passed", "passed", issues=["x"]), "key": key},
        {**result("passed", "passed"), "key": key, "pins": result("passed", "passed")["pins"][:1]},
        [],
    ):
        path.write_text(json.dumps(planted))
        assert runner._cached(settings_, key, pins) is None, planted
    path.write_text("{not json")
    assert runner._cached(settings_, key, pins) is None
    assert runner._cached(runner.Settings(uv="uv", git="git", work=tmp_path), key, pins) is None


def test_runner_version_covers_the_runner_and_the_oracle():
    assert {path.name for path in runner.RUNNER_FILES} == {"pin_runner.py", "axiom_pin_oracle.py"}
    assert all(path.is_file() for path in runner.RUNNER_FILES)
    assert re.fullmatch(r"[0-9a-f]{64}", runner.runner_version())


# ---------------------------------------------------------------------------
# The alarm summary reads the report as data


def test_summarize_prints_only_validated_fields(tmp_path):
    report = {
        "commits": [
            {
                "repo": "PolicyEngine/a",
                "commit": COMMIT,
                "issues": [],
                "pins": [
                    {"entry": "us:x", "label": "entries[0].tests[0]", "id": "tests/test_a.py::test_x", "outcome": "passed"},
                    {"entry": "us:x", "label": "entries[0].tests[1]", "id": "tests/test_a.py::TestK::test_y", "outcome": "skipped"},
                    {"entry": "us:x", "label": "entries[0].tests[2]", "id": "tests/test_a.py::test_z`@team", "outcome": "failed"},
                    {"entry": "us:x", "label": "entries[0].tests[3]", "id": "tests/test_a.py::test_w", "outcome": "pwned <script>"},
                ],
            },
            {"repo": "x/y`; rm -rf", "commit": COMMIT, "pins": []},
        ],
        "unrun": [{"entry": "us:x", "label": "entries[0].tests[4]", "reason": "@everyone look"}],
        "issues": ["x"],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    lines = runner.summarize(path)
    assert lines == [
        "- `us:x` entries[0].tests[1]: `PolicyEngine/a@cccccccccccc:tests/test_a.py::TestK::test_y` skipped",
        "- `us:x` entries[0].tests[4]: the runner can't execute this pin.",
        "- 3 malformed report item(s); the `pinned-tests` job's log has the details.",
    ]
    assert not any("@team" in line or "pwned" in line or "rm -rf" in line or "@everyone" in line for line in lines)


def test_summarize_without_a_report(tmp_path):
    assert "No readable pinned-test report" in runner.summarize(tmp_path / "absent.json")[0]
    (tmp_path / "list.json").write_text("[]")
    assert "not a JSON object" in runner.summarize(tmp_path / "list.json")[0]
    (tmp_path / "clean.json").write_text(json.dumps({"commits": [], "unrun": [], "issues": []}))
    assert runner.summarize(tmp_path / "clean.json") == ["No pinned test failed."]
    (tmp_path / "error.json").write_text(json.dumps({"commits": [], "unrun": [], "issues": ["runner error: x"]}))
    assert "1 issue(s) outside any pin" in runner.summarize(tmp_path / "error.json")[0]


def test_summarize_cli(tmp_path, capsys):
    (tmp_path / "clean.json").write_text(json.dumps({"commits": [], "unrun": [], "issues": []}))
    assert runner.main(["--summarize", str(tmp_path / "clean.json")]) == 0
    assert capsys.readouterr().out == "No pinned test failed.\n"


# ---------------------------------------------------------------------------
# The workflow isolates the job that runs other repositories' code


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def steps_text(job) -> str:
    return yaml.safe_dump(job, sort_keys=True)


def test_pinned_tests_job_holds_no_secrets():
    job = workflow()["jobs"]["pinned-tests"]
    assert job["permissions"] == {"contents": "read"}  # no id-token, no write
    text = steps_text(job)
    for forbidden in ("secrets.", "github.token", "GITHUB_TOKEN", "GH_TOKEN", "R2_", "ACTIONS_ID_TOKEN", "vars."):
        assert forbidden not in text, forbidden
    assert "env" not in job
    for step in job["steps"]:
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False, step


def test_pinned_tests_job_passes_its_result_only_as_a_json_artifact():
    jobs = workflow()["jobs"]
    job = jobs["pinned-tests"]
    assert "outputs" not in job
    text = steps_text(job)
    for channel in ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH", "GITHUB_STEP_SUMMARY"):
        assert channel not in text, channel
    uploads = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["name"] == "pinned-tests"
    assert uploads[0]["with"]["path"].endswith("/pinned-tests.json")
    assert "needs.pinned-tests.outputs" not in WORKFLOW.read_text()
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert "tools/pin_runner.py" in runs and "--report" in runs
    tests = [step for step in job["steps"] if "tests/test_pin_runner.py" in step.get("run", "")]
    assert tests and tests[0]["env"]["AXIOM_PIN_RUNNER_TESTS_REQUIRED"] == "1"


def test_only_the_isolated_job_runs_other_repositories_code():
    jobs = workflow()["jobs"]
    for name, job in jobs.items():
        runs = " ".join(step.get("run", "") for step in job.get("steps", []))
        runs_pins = "tools/pin_runner.py --root" in runs or "pin_runner.py --register" in runs
        assert runs_pins == (name == "pinned-tests"), name


def test_the_report_is_read_as_data():
    jobs = workflow()["jobs"]
    readers = [
        name for name, job in jobs.items()
        if any(
            str(step.get("uses", "")).startswith("actions/download-artifact@") and step["with"]["name"] == "pinned-tests"
            for step in job.get("steps", [])
        )
    ]
    assert readers == ["alarm"]
    for name in readers:
        for step in jobs[name]["steps"]:
            assert "${{" not in step.get("run", ""), (name, step.get("name"))
        runs = " ".join(step.get("run", "") for step in jobs[name]["steps"])
        assert re.search(r"pin_runner\.py\s*\\?\s*--summarize", runs)
    assert set(jobs["alarm"]["needs"]) == {"verify", "pinned-tests"}


def test_workflow_permissions_default_to_read():
    assert workflow()["permissions"] == {"contents": "read"}


# ---------------------------------------------------------------------------
# The whole runner on fixture repositories

PYPROJECT = textwrap.dedent(
    """\
    [project]
    name = "fixture"
    version = "0"
    requires-python = ">=3.10"

    [project.optional-dependencies]
    dev = [{dev}]

    [build-system]
    requires = ["setuptools>=68"]
    build-backend = "setuptools.build_meta"

    [tool.setuptools]
    py-modules = []
    {extra}
    """
)

MAIN_TESTS = textwrap.dedent(
    """\
    import os

    import pytest


    def test_ok():
        assert True


    def test_no_secrets():
        assert "AXIOM_FIXTURE_SECRET" not in os.environ
        assert "GITHUB_TOKEN" not in os.environ
        assert os.environ["HOME"] != __REAL_HOME__


    class TestK:
        def test_m(self):
            pass


    @pytest.mark.parametrize("x", [1, 2])
    def test_p(x):
        assert x


    @pytest.mark.parametrize("x", [1, 2])
    def test_p_half(x):
        assert x == 1


    @pytest.mark.skip(reason="planted")
    def test_skipped():
        pass


    @pytest.mark.xfail(reason="planted")
    def test_xfailed():
        assert False


    @pytest.mark.xfail(reason="planted")
    def test_xpassed():
        pass


    @pytest.mark.xfail(reason="planted", strict=True)
    def test_strict_xpass():
        pass


    def test_fails():
        assert False


    def test_setup_error(broken):
        pass


    def test_conftest_skipped():
        pass


    def test_body_skip():
        pytest.skip("not today")
    """
)

CONFTEST = textwrap.dedent(
    """\
    import pytest


    @pytest.fixture
    def broken():
        raise RuntimeError("fixture error")


    def pytest_collection_modifyitems(items):
        for item in items:
            if item.name == "test_conftest_skipped":
                item.add_marker(pytest.mark.skip(reason="conftest says no"))
    """
)

# Round 6's shape: an alias of pytest.skip called at import skips the module.
PARTIAL_SKIP = textwrap.dedent(
    """\
    import pytest
    from functools import partial

    skip_if_missing = partial(pytest.skip, "optional data absent", allow_module_level=True)
    skip_if_missing()


    def test_x():
        pass
    """
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@invalid",
    }
    return subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def fixture_repo(base: Path, repo: str, files: dict[str, str], *, dev='"pytest==9.1.1"', extra="") -> str:
    """A git repository at base/<owner>/<name>; returns its one commit."""

    root = base / repo
    root.mkdir(parents=True)
    files = {"pyproject.toml": PYPROJECT.format(dev=dev, extra=extra), **files}
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def settings(tmp_path: Path, base: Path, **overrides) -> object:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return runner.Settings(
        uv=UV, git=GIT, work=work, repo_url=f"file://{base}/{{repo}}", uv_dirs=runner.uv_dirs(UV), **overrides
    )


def pinned(repo: str, commit: str, ids: list[str]) -> list[dict]:
    return [
        {"repo": repo, "commit": commit, "path": test_id.split("::", 1)[0], "name": test_id.split("::", 1)[1], "pins": ["reading"]}
        for test_id in ids
    ]


def outcomes(report) -> dict[str, str]:
    return {pin["id"]: pin["outcome"] for result in report["commits"] for pin in result["pins"]}


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    base = tmp_path_factory.mktemp("repos")
    commits = {
        "main": fixture_repo(base, "PolicyEngine/fixture-main", {
            "tests/conftest.py": CONFTEST,
            "tests/test_main.py": MAIN_TESTS.replace("__REAL_HOME__", repr(os.environ.get("HOME", ""))),
            "tests/test_partial_skip.py": PARTIAL_SKIP,
        }),
        "addopts": fixture_repo(
            base,
            "PolicyEngine/fixture-addopts",
            {"tests/test_sel.py": "import pytest\n\n\n@pytest.mark.slow\ndef test_slow():\n    pass\n\n\ndef test_fast():\n    pass\n"},
            extra='\n[tool.pytest.ini_options]\naddopts = "-m \'not slow\'"\nmarkers = ["slow: slow tests"]\n',
        ),
        "broken": fixture_repo(base, "TheAxiomFoundation/fixture-broken", {
            "tests/test_broken.py": "def test_x(:\n    pass\n",
            "tests/test_fine.py": "def test_y():\n    pass\n",
        }),
        "install": fixture_repo(
            base, "PolicyEngine/fixture-install", {"tests/test_a.py": "def test_a():\n    pass\n"},
            dev='"pytest==9.1.1", "axiom-no-such-package-for-pin-runner-tests==1.0"',
        ),
        "nopytest": fixture_repo(
            base, "PolicyEngine/fixture-nopytest", {"tests/test_a.py": "def test_a():\n    pass\n"}, dev="",
        ),
        "slow": fixture_repo(base, "PolicyEngine/fixture-slow", {
            "tests/test_slow.py": "import time\n\n\ndef test_sleep():\n    time.sleep(600)\n",
        }),
    }
    return base, commits


MAIN_EXPECTED = {
    "tests/test_main.py::test_ok": "passed",
    "tests/test_main.py::test_no_secrets": "passed",
    "tests/test_main.py::TestK::test_m": "passed",
    "tests/test_main.py::test_p": "passed",
    "tests/test_main.py::test_p_half": "failed",
    "tests/test_main.py::test_skipped": "skipped",
    "tests/test_main.py::test_xfailed": "xfail",
    "tests/test_main.py::test_xpassed": "xpass",
    "tests/test_main.py::test_strict_xpass": "failed",
    "tests/test_main.py::test_fails": "failed",
    "tests/test_main.py::test_setup_error": "error",
    "tests/test_main.py::test_conftest_skipped": "skipped",
    "tests/test_main.py::test_body_skip": "skipped",
    "tests/test_main.py::test_absent": "missing",
    "tests/test_partial_skip.py::test_x": "not collected",
    "tests/test_nofile.py::test_x": "missing file",
}


@needs_tools
def test_every_way_a_pin_fails_closed(tmp_path, fixtures, monkeypatch):
    base, commits = fixtures
    monkeypatch.setenv("AXIOM_FIXTURE_SECRET", "s3cret")
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret")
    payload = register(*pinned("PolicyEngine/fixture-main", commits["main"], list(MAIN_EXPECTED)))
    report = runner.run_register(payload, settings(tmp_path, base))
    assert outcomes(report) == MAIN_EXPECTED
    (result,) = report["commits"]
    env = result["environment"]
    assert env["install"] == ["-e", ".[dev]"] and env["python_request"] == "3.14" and env["requires_python"] == ">=3.10"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", env["exclude_newer"])
    assert env["pytest"] == "9.1.1" and env["inifile"] == "pyproject.toml"
    assert report["runner"]["uv"].startswith("uv ")
    by_id = {pin["id"]: pin for pin in result["pins"]}
    assert [case["outcome"] for case in by_id["tests/test_main.py::test_p"]["cases"]] == ["passed", "passed"]
    # test_p_half's first case passed; the pin did not.
    assert report["summary"] == {"pins": 16, "passed": 4, "cases_passed": 6, "issues": 12}
    assert len(report["issues"]) == 12


@needs_tools
def test_repository_deselection_fails_the_pin(tmp_path, fixtures):
    base, commits = fixtures
    payload = register(*pinned("PolicyEngine/fixture-addopts", commits["addopts"], ["tests/test_sel.py::test_slow", "tests/test_sel.py::test_fast"]))
    report = runner.run_register(payload, settings(tmp_path, base))
    assert outcomes(report) == {"tests/test_sel.py::test_slow": "deselected", "tests/test_sel.py::test_fast": "passed"}
    assert report["commits"][0]["environment"]["addopts"] == ["-m", "not slow"]


@needs_tools
def test_a_collection_error_fails_every_pin_of_the_commit(tmp_path, fixtures):
    # pytest stops before running anything when a file fails to collect.
    base, commits = fixtures
    payload = register(*pinned("TheAxiomFoundation/fixture-broken", commits["broken"], ["tests/test_broken.py::test_x", "tests/test_fine.py::test_y"]))
    report = runner.run_register(payload, settings(tmp_path, base))
    assert outcomes(report) == {"tests/test_broken.py::test_x": "collection error", "tests/test_fine.py::test_y": "not run"}


@needs_tools
def test_an_install_failure_fails_every_pin(tmp_path, fixtures):
    base, commits = fixtures
    payload = register(*pinned("PolicyEngine/fixture-install", commits["install"], ["tests/test_a.py::test_a"]))
    report = runner.run_register(payload, settings(tmp_path, base))
    assert outcomes(report) == {"tests/test_a.py::test_a": "install failed"}
    assert report["commits"][0]["logs"]["install"]


@needs_tools
def test_an_install_without_pytest_fails_every_pin(tmp_path, fixtures):
    base, commits = fixtures
    payload = register(*pinned("PolicyEngine/fixture-nopytest", commits["nopytest"], ["tests/test_a.py::test_a"]))
    report = runner.run_register(payload, settings(tmp_path, base))
    assert outcomes(report) == {"tests/test_a.py::test_a": "install failed"}
    assert report["issues"][0].endswith("the install (-e .[dev]) does not provide pytest")


@needs_tools
def test_a_timeout_fails_the_pin(tmp_path, fixtures):
    base, commits = fixtures
    payload = register(*pinned("PolicyEngine/fixture-slow", commits["slow"], ["tests/test_slow.py::test_sleep"]))
    report = runner.run_register(payload, settings(tmp_path, base, timeout=20))
    assert outcomes(report) == {"tests/test_slow.py::test_sleep": "timed out"}
    assert report["commits"][0]["seconds"]["total"] < 40


@needs_tools
def test_an_unfetchable_commit_fails_the_pin(tmp_path, fixtures):
    base, _ = fixtures
    payload = register(*pinned("PolicyEngine/fixture-main", "e" * 40, ["tests/test_main.py::test_ok"]))
    report = runner.run_register(payload, settings(tmp_path, base))
    assert outcomes(report) == {"tests/test_main.py::test_ok": "fetch failed"}


@needs_tools
def test_a_repository_off_the_allowlist_never_runs(tmp_path, fixtures):
    base, commits = fixtures
    # The commit exists under an allowed owner; a copy elsewhere must not be fetched.
    shutil.copytree(base / "PolicyEngine" / "fixture-main", base / "SomeoneElse" / "fixture-main")
    payload = register(*pinned("SomeoneElse/fixture-main", commits["main"], ["tests/test_main.py::test_ok"]))
    report = runner.run_register(payload, settings(tmp_path, base))
    assert report["commits"] == []
    assert report["issues"] == [
        "us:fixture entries[0].tests[0] tests/test_main.py::test_ok: the runner won't execute pins in "
        "SomeoneElse/fixture-main (allowed owners: TheAxiomFoundation, PolicyEngine)."
    ]
    assert not any((tmp_path / "work").iterdir())


@needs_tools
def test_passing_results_are_cached_by_runner_version(tmp_path, fixtures, monkeypatch):
    base, commits = fixtures
    payload = register(*pinned("PolicyEngine/fixture-main", commits["main"], ["tests/test_main.py::test_ok"]))
    cache = tmp_path / "cache"
    first = runner.run_register(payload, settings(tmp_path, base, cache_dir=cache))
    second = runner.run_register(payload, settings(tmp_path, base, cache_dir=cache))
    assert [r["cached"] for r in first["commits"] + second["commits"]] == [False, True]
    assert outcomes(second) == {"tests/test_main.py::test_ok": "passed"}
    monkeypatch.setattr(runner, "runner_version", lambda: "another runner")
    third = runner.run_register(payload, settings(tmp_path, base, cache_dir=cache))
    assert third["commits"][0]["cached"] is False
    # Failures are never cached.
    failing = register(*pinned("PolicyEngine/fixture-main", commits["main"], ["tests/test_main.py::test_skipped"]))
    for _ in range(2):
        assert runner.run_register(failing, settings(tmp_path, base, cache_dir=cache))["commits"][0]["cached"] is False


@needs_tools
def test_cli_writes_the_report_and_fails_on_any_issue(tmp_path, fixtures, capsys):
    base, commits = fixtures
    payload = register(*pinned("PolicyEngine/fixture-main", commits["main"], ["tests/test_main.py::test_ok", "tests/test_main.py::test_skipped"]))
    register_path = tmp_path / "register.yaml"
    register_path.write_text(yaml.safe_dump(payload))
    report_path = tmp_path / "out" / "report.json"
    code = runner.main(["--register", str(register_path), "--report", str(report_path), "--repo-url", f"file://{base}/{{repo}}"])
    assert code == 1
    report = json.loads(report_path.read_text())
    assert outcomes(report) == {"tests/test_main.py::test_ok": "passed", "tests/test_main.py::test_skipped": "skipped"}
    captured = capsys.readouterr()
    assert "1 of 2 pin(s) passed" in captured.out
    assert "tests/test_main.py::test_skipped: skipped" in captured.err
    assert runner.summarize(report_path) == [
        f"- `us:fixture` entries[0].tests[1]: `PolicyEngine/fixture-main@{commits['main'][:12]}:tests/test_main.py::test_skipped` skipped"
    ]


def test_cli_fails_closed_without_a_register(tmp_path):
    report_path = tmp_path / "report.json"
    assert runner.main(["--register", str(tmp_path / "absent.yaml"), "--report", str(report_path)]) == 1
    report = json.loads(report_path.read_text())
    assert report["issues"][0].startswith("runner error:")
