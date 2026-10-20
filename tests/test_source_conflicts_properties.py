"""Property-based and differential tests for tools/source_conflicts.py.

Invariants, for every input:

1. Acceptance: every generated well-formed entry (any status, with the
   ruling and confirmation its status requires, any mix of corpus and pending
   texts) passes `check`.
2. Status rules: a ruling present on an open entry, or missing from a ruled,
   confirmed or contradicted one, is always reported; so is a confirmation on
   an open or ruled entry or missing from a confirmed or contradicted one.
3. Totality: `check` returns a list of strings for any YAML-shaped input and
   never raises, including a well-formed entry with any one known field (at
   any depth) replaced by an arbitrary YAML value.
4. Round trip: a register entry turned into a module interpretation and
   written into a module passes the adopting-module check, and its
   interpretation core equals the entry's.
5. Drift: changing any one core field in the module's copy is reported, and
   the report names that field; for a confirmed entry that includes any one
   field of its confirmation record.
6. Hygiene: a decision-queue id, session id, local path or email address in
   any free-text field is reported; hex digests in commit and sha256 fields
   never are.
7. Aggregation: global ids in the output are unique, the rows do not depend on
   the order registers are given, and each distinct global id appears once.
8. Snapshot text is whitespace-normalised and stable: re-reading its own
   output as plain text returns it unchanged.
9. Differential (when axiom-encode with module.interpretations support is
   importable): `check_module_interpretation` and axiom-encode's
   `_validate_module_interpretations` agree on whether an entry is valid, for
   well-formed entries and for single-field mutations of them.
10. Matcher consistency (when axiom-encode is importable): every run of whole
    words taken from a snapshot's text matches it under axiom-encode's
    proof-atom matcher.
11. Differential, both directions: for generated test modules (fixtures,
    marks, parametrize, Hypothesis, classes with and without bases or
    constructors, `__test__` in any form, pytestmark, module-level skips, a
    main guard's else branch, a shadowed `object`, a module `__getattr__`,
    annotations, generator and async tests, rebinding, and skips in bodies,
    fixtures, setup functions, marks, parameter sets and Hypothesis phases,
    import-time calls the screen cannot follow, project helpers called at
    import, unreachable branches): an accepted, unwarned name is one pytest
    collects; every collected name is accepted except the deliberate refusals
    in `REFUSED_BUT_COLLECTED`; an accepted test that pytest does not collect
    or does not run is always warned; and every visible skip planted is
    warned. A warning may also fire on a test that runs. This is evidence for
    a screen over the shapes generated, not a proof over all Python.

Hypothesis is not installed in the shared validate workflow's pytest leg, so
these skip there; the source-conflicts workflow installs it and runs them.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import string
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "source_conflicts.py"
_spec = importlib.util.spec_from_file_location("source_conflicts", TOOL_PATH)
sc = importlib.util.module_from_spec(_spec)
sys.modules["source_conflicts"] = sc
_spec.loader.exec_module(sc)

REPO = "TheAxiomFoundation/rulespec-us"
SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

# Free text without digits, '@', '/' or '~', so it never trips hygiene by accident.
prose = st.text(alphabet=string.ascii_letters + " ,.;:'()-", min_size=1, max_size=40).filter(lambda s: s.strip())
# A slug such as "d1338" reads as a decision-queue id, which hygiene rightly
# rejects (an intended violation), so generated slugs avoid that shape.
kebab = st.from_regex(r"[a-z0-9]{1,6}(-[a-z0-9]{1,6}){0,3}", fullmatch=True).filter(
    lambda s: not __import__("re").search(r"\bd[0-9]{3,5}\b", s)
)
hexs = lambda n: st.text(alphabet="0123456789abcdef", min_size=n, max_size=n)  # noqa: E731
dates = st.dates(min_value=__import__("datetime").date(1990, 1, 1), max_value=__import__("datetime").date(2040, 12, 31))
corpus_paths = st.builds(
    lambda j, c, segs: "/".join([j, c, *segs]),
    st.sampled_from(["us", "us-co", "uk-babergh"]),
    st.sampled_from(["statute", "regulation", "guidance", "manual"]),
    st.lists(st.from_regex(r"[a-z0-9][a-z0-9.-]{0,5}", fullmatch=True), min_size=1, max_size=4),
).filter(lambda path: not any(pattern.search(path) for pattern, _ in sc.HYGIENE_PATTERNS))  # e.g. us/statute/d000
urls = st.builds(lambda host, path: f"https://{host}.org/{path}", st.from_regex(r"[a-z]{3,8}", fullmatch=True), kebab)
texts = st.lists(st.fixed_dictionaries({"corpus_citation_path": corpus_paths, "excerpt": prose}), max_size=3)
pending = st.lists(
    st.fixed_dictionaries(
        {
            "citation": prose,
            "url": urls,
            "snapshot": st.fixed_dictionaries(
                {
                    "repo": st.just("PolicyEngine/microcosm-dynamics"),
                    "commit": hexs(40),
                    "path": st.builds(lambda k: f"tests/data/{k}.html", kebab),
                    "sha256": hexs(64),
                }
            ),
            "excerpt": prose,
        }
    ),
    max_size=3,
)
alternatives = st.lists(
    st.one_of(
        st.fixed_dictionaries({"reading": prose}),
        st.fixed_dictionaries({"reading": prose, "effect": prose}),
    ),
    min_size=1,
    max_size=3,
)
records = st.fixed_dictionaries({"by": prose, "date": st.one_of(dates, dates.map(str)), "record": urls})
confirmations = st.fixed_dictionaries(
    {"by": prose, "date": dates.map(str), "excerpt": prose}, optional={"record": urls}
)


@st.composite
def entries(draw, jurisdiction="us"):
    status = draw(st.sampled_from(sc.STATUSES[:3]))  # contradicted is always a finding
    body_texts = draw(texts)
    body_pending = draw(pending)
    assume(body_texts or body_pending)
    alts = draw(alternatives)
    value = {
        "id": draw(kebab),
        "jurisdiction": jurisdiction,
        "program": draw(kebab),
        "question": draw(prose),
        "texts": body_texts,
        "reading": draw(prose),
        "alternatives": alts,
        "basis": draw(prose),
        "status": status,
        "outcome": draw(st.sampled_from(sc.OUTCOMES)),
    }
    if body_pending:
        value["pending_texts"] = body_pending
    if status != "open":
        value["ruling"] = draw(records)
    if status == "confirmed":
        value["confirmation"] = draw(confirmations)
    if draw(st.booleans()):
        commit = draw(hexs(40))
        value["implementations"] = [
            {
                "repo": "PolicyEngine/microcosm-dynamics",
                "commit": commit,
                "path": "src/x.py",
                "follows": draw(st.sampled_from(["reading", *[f"alternatives[{i}]" for i in range(len(alts))]])),
                "cites_entry": draw(st.booleans()),
            }
        ]
        value["tests"] = [
            {"repo": "PolicyEngine/microcosm-dynamics", "commit": commit, "path": "tests/test_x.py", "name": "test_default", "pins": ["reading"]}
        ] + [
            {"repo": "PolicyEngine/microcosm-dynamics", "commit": commit, "path": "tests/test_x.py", "name": f"test_alt_{i}", "pins": [f"alternatives[{i}]"]}
            for i in range(len(alts))
        ]
    if value.get("tests") and draw(st.booleans()):
        value["money_case"] = {
            "description": draw(prose),
            "amount": draw(st.from_regex(r"(0|[1-9][0-9]{0,4})(\.[0-9]{2})?", fullmatch=True)),
            "currency": "USD",
            "period": draw(st.sampled_from(sc.PERIODS)),
            "computed_by": draw(st.sampled_from([test["name"] for test in value["tests"]])),
        }
    return value


def repo(tmp_path: Path) -> sc.RepoContext:
    for directory in ("us/statutes", "us-co/regulations"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    return sc.RepoContext(root=tmp_path, repo=REPO)


# 1. Acceptance
@SETTINGS
@given(value=entries())
def test_generated_entries_pass(tmp_path, value):
    assert sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False) == []


# 2. Status rules
@SETTINGS
@given(value=entries(), status=st.sampled_from(sc.STATUSES), ruling=records, confirmation=confirmations, keep_ruling=st.booleans(), keep_confirmation=st.booleans())
def test_status_rules(tmp_path, value, status, ruling, confirmation, keep_ruling, keep_confirmation):
    value = copy.deepcopy(value)
    value["status"] = status
    value.pop("ruling", None)
    value.pop("confirmation", None)
    if keep_ruling:
        value["ruling"] = ruling
    if keep_confirmation:
        value["confirmation"] = confirmation
    issues = sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False)
    ruling_wrong = keep_ruling != (status != "open")
    confirmation_wrong = keep_confirmation != (status in {"confirmed", "contradicted"})
    assert any(".ruling must be" in issue for issue in issues) == ruling_wrong
    assert any(".confirmation must be" in issue for issue in issues) == confirmation_wrong
    assert any("contradicted this reading" in issue for issue in issues) == (status == "contradicted")


# 3. Totality
yaml_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8), dates),
    lambda children: st.one_of(st.lists(children, max_size=4), st.dictionaries(st.text(max_size=8), children, max_size=4)),
    max_leaves=25,
)


@SETTINGS
@given(payload=st.one_of(yaml_values, st.fixed_dictionaries({"version": st.just(1), "entries": st.lists(yaml_values, max_size=3)})))
def test_check_is_total(tmp_path, payload):
    issues = sc.check_register(payload, repo(tmp_path), scan_modules=True)
    assert isinstance(issues, list) and all(isinstance(issue, str) for issue in issues)


# Totality where it matters most: a well-formed entry with one known field,
# at any depth, replaced by an arbitrary YAML value. Random keys rarely hit
# the schema's own (often long) field names, so target them directly.
FIELD_PATHS = [
    (key,) for key in sorted(sc.ENTRY_KEYS)
] + [
    ("texts", 0, "corpus_citation_path"), ("texts", 0, "excerpt"),
    ("pending_texts", 0, "citation"), ("pending_texts", 0, "url"), ("pending_texts", 0, "snapshot"),
    ("pending_texts", 0, "snapshot", "repo"), ("pending_texts", 0, "snapshot", "commit"),
    ("pending_texts", 0, "snapshot", "sha256"), ("alternatives", 0, "reading"), ("alternatives", 0, "effect"),
    ("ruling", "by"), ("ruling", "date"), ("ruling", "record"),
    ("implementations", 0, "repo"), ("implementations", 0, "commit"), ("implementations", 0, "path"),
    ("implementations", 0, "follows"), ("implementations", 0, "cites_entry"),
    ("tests", 0, "repo"), ("tests", 0, "commit"), ("tests", 0, "path"), ("tests", 0, "name"), ("tests", 0, "pins"),
    ("money_case", "amount"), ("money_case", "currency"), ("money_case", "period"), ("money_case", "description"),
    ("money_case", "computed_by"),
]


def _set_path(value, path, new):
    target = value
    for key in path[:-1]:
        if isinstance(target, dict) and key in target:
            target = target[key]
        elif isinstance(target, list) and isinstance(key, int) and key < len(target):
            target = target[key]
        else:
            return False
    if isinstance(target, dict) or (isinstance(target, list) and isinstance(path[-1], int) and path[-1] < len(target)):
        target[path[-1]] = new
        return True
    return False


@SETTINGS
@given(value=entries(), path=st.sampled_from(FIELD_PATHS), new=yaml_values)
def test_check_is_total_on_malformed_known_fields(tmp_path, value, path, new):
    value = copy.deepcopy(value)
    assume(_set_path(value, path, new))
    issues = sc.check_register({"version": 1, "entries": [value, copy.deepcopy(value)]}, repo(tmp_path), scan_modules=True)
    assert isinstance(issues, list) and all(isinstance(issue, str) for issue in issues)


# 4 and 5. Round trip and drift
@SETTINGS
@given(value=entries(), field=st.sampled_from([f for f in sc.INTERPRETATION_CORE if f != "confirmation"]))
def test_round_trip_and_drift(tmp_path, value, field):
    ctx = repo(tmp_path)
    value = copy.deepcopy(value)
    value.pop("pending_texts", None)
    assume(value["texts"])
    module = "us/statutes/42/403.yaml"
    value["adopted_by"] = [{"module": module, "interpretation": value["id"]}]
    # The module is the only adopter here; an implementation following an
    # alternative would need its own pinning test.
    value.pop("implementations", None)
    value["tests"] = [{"path": "us/statutes/42/403.test.yaml", "name": "case", "pins": ["reading"]}]
    if "money_case" in value:
        value["money_case"]["computed_by"] = "case"
    carried = sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["rule_a"])
    assert sc.interpretation_core(carried) == sc.interpretation_core(value)
    path = ctx.root / module
    path.parent.mkdir(parents=True, exist_ok=True)
    (ctx.root / "us/statutes/42/403.test.yaml").write_text(yaml.safe_dump([{"name": "case"}]))

    def write(item):
        path.write_text(yaml.safe_dump({"format": "rulespec/v1", "module": {"interpretations": [item]}, "rules": [{"name": "rule_a"}]}))

    write(carried)
    assert sc.check_register({"version": 1, "entries": [value]}, ctx) == []
    drifted = copy.deepcopy(carried)
    drifted[field] = {"texts": [], "alternatives": [{"reading": "different"}]}.get(field, "different")
    if field == "status":
        drifted["status"] = "open" if value["status"] != "open" else "ruled"
    if field == "ruling":
        drifted["ruling"] = {**(value.get("ruling") or {}), "by": "Someone Else Organisation"}
    assume(drifted[field] != carried.get(field))
    write(drifted)
    issues = sc.check_register({"version": 1, "entries": [value]}, ctx)
    assert any(f"`{field}` differs" in issue for issue in issues)


# 5b. Drift of a confirmation record: a confirmed entry whose module copy
# carries a different confirmation is reported, naming the field.
@SETTINGS
@given(value=entries(), confirmation=confirmations, field=st.sampled_from(["by", "date", "excerpt", "record"]), other=prose)
def test_confirmation_drift(tmp_path, value, confirmation, field, other):
    ctx = repo(tmp_path)
    value = copy.deepcopy(value)
    value.pop("pending_texts", None)
    assume(value["texts"])
    value["status"] = "confirmed"
    value.setdefault("ruling", {"by": "Org", "date": "2026-10-12", "record": "https://example.org/r"})
    value["confirmation"] = dict(confirmation)
    module = "us/statutes/42/403.yaml"
    value["adopted_by"] = [{"module": module, "interpretation": value["id"]}]
    value.pop("implementations", None)
    value["tests"] = [{"path": "us/statutes/42/403.test.yaml", "name": "case", "pins": ["reading"]}]
    if "money_case" in value:
        value["money_case"]["computed_by"] = "case"
    carried = sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["rule_a"])
    drifted = copy.deepcopy(carried)
    replacement = {"date": "2031-01-02", "record": "https://example.org/other"}.get(field, other)
    assume(drifted["confirmation"].get(field) != replacement)
    drifted["confirmation"][field] = replacement
    path = ctx.root / module
    path.parent.mkdir(parents=True, exist_ok=True)
    (ctx.root / "us/statutes/42/403.test.yaml").write_text(yaml.safe_dump([{"name": "case"}]))
    path.write_text(yaml.safe_dump({"format": "rulespec/v1", "module": {"interpretations": [drifted]}, "rules": [{"name": "rule_a"}]}))
    issues = sc.check_register({"version": 1, "entries": [value]}, ctx)
    assert any("`confirmation` differs" in issue for issue in issues), issues


# 6. Hygiene
private = st.sampled_from(["d1338", "local_6085d52a", "/Users/x/y", "~/notes", "a.b@example.org"])


@SETTINGS
@given(value=entries(), token=private, field=st.sampled_from(["question", "reading", "basis"]))
def test_hygiene(tmp_path, value, token, field):
    value = copy.deepcopy(value)
    value[field] = f"{value[field]} {token} end"
    issues = sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False)
    assert any("the register is public" in issue for issue in issues)


@SETTINGS
@given(value=entries(), digest=hexs(64))
def test_hygiene_ignores_digests(tmp_path, value, digest):
    value = copy.deepcopy(value)
    for item in value.get("pending_texts", []):
        item["snapshot"]["sha256"] = digest
        item["snapshot"]["commit"] = digest[:40]
    issues = sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False)
    assert not any("the register is public" in issue for issue in issues)


# 7. Aggregation
@SETTINGS
@given(us=st.lists(entries(), max_size=4), uk=st.lists(entries(jurisdiction="uk"), max_size=4))
def test_aggregate(us, uk):
    registers = {"TheAxiomFoundation/rulespec-us": {"entries": us}, "TheAxiomFoundation/rulespec-uk": {"entries": uk}}
    rows, issues = sc.aggregate(registers)
    reversed_rows, _ = sc.aggregate(dict(reversed(list(registers.items()))))
    ids = [row["global_id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert rows == reversed_rows
    assert set(ids) == {sc.global_id(e) for e in us + uk}
    assert not any("outside that repository" in issue for issue in issues)


# 8. Snapshot text
@SETTINGS
@given(parts=st.lists(st.one_of(prose, st.sampled_from(["<p>", "</p>", "<b>", "</b>", "<br/>", "&amp;", "&#8217;", "\n\n", "\t"])), max_size=20))
def test_snapshot_text_normalised(parts):
    text = sc.snapshot_text("".join(parts).encode(), "x.html")
    assert text == text.strip() and "  " not in text and "\n" not in text and "<" not in text
    assert sc.snapshot_text(text.encode(), "x.txt") == text


# 9. Differential against axiom-encode
def _encoder_validator():
    proof_validator = pytest.importorskip("axiom_encode.harness.proof_validator")
    validator = getattr(proof_validator, "_validate_module_interpretations", None)
    if validator is None:
        pytest.skip("this axiom-encode predates module.interpretations")
    return validator


MUTATIONS = [
    lambda item: item.pop("question"),
    lambda item: item.__setitem__("reading", "   "),
    lambda item: item.__setitem__("alternatives", []),
    lambda item: item.__setitem__("texts", []),
    lambda item: item.__setitem__("status", "pending"),
    lambda item: item.__setitem__("id", "Bad_Id"),
    lambda item: item.__setitem__("affects", ["no_such_rule"]),
    lambda item: item.__setitem__("affects", []),
    lambda item: item.__setitem__("extra", 1),
    lambda item: item["alternatives"][0].__setitem__("why", "x"),
    lambda item: item["texts"][0].__setitem__("note", "x") if item.get("texts") else item.__setitem__("texts", []),
    lambda item: item.__setitem__("ruling", {"by": "Org", "date": "2026-02-30", "record": "https://x.org/1"}),
    lambda item: item.__setitem__("ruling", {"by": "Org", "date": "2026-02-03", "record": "http://x.org/1"}),
    lambda item: item.__setitem__("confirmation", {"by": "Org", "date": "2026-02-03"}),
]


@SETTINGS
@given(value=entries(), mutation=st.one_of(st.none(), st.sampled_from(range(len(MUTATIONS)))))
def test_matches_encoder_interpretation_rules(value, mutation):
    validator = _encoder_validator()
    assume(value["texts"])
    item = sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["rule_a"])
    if mutation is not None:
        MUTATIONS[mutation](item)
    ours = sc.check_module_interpretation(item, rule_names={"rule_a"}, label="x")
    theirs = validator({"module": {"interpretations": [item]}, "rules": [{"name": "rule_a"}]}, source_texts=None)
    assert bool(ours) == bool(theirs), (ours, theirs)


# 10. Matcher consistency
@SETTINGS
@given(words=st.lists(st.from_regex(r"[A-Za-z][a-z]{0,8}[.,;]?", fullmatch=True), min_size=3, max_size=30), data=st.data())
def test_word_runs_match_under_encoder_matcher(words, data):
    pytest.importorskip("axiom_encode.harness.proof_validator")
    matches = sc.encoder_matcher()
    html = "<p>" + " ".join(f"<b>{w}</b>" if i % 4 == 0 else w for i, w in enumerate(words)) + "</p>"
    text = sc.snapshot_text(html.encode(), "x.html")
    tokens = text.split(" ")
    start = data.draw(st.integers(0, len(tokens) - 1))
    end = data.draw(st.integers(start + 1, len(tokens)))
    assert matches(text, " ".join(tokens[start:end]))


# 11. Differential, both directions: the test-reference screen against
# pytest itself. Each example writes one to three generated test modules and
# runs pytest on them in a subprocess (Hypothesis tests inside a Hypothesis
# test do not run in-process), next to a `helpers_mod` that stands in for
# project code. A plugin records every collected test, every test whose call
# phase passed (not skipped, not xfail), and every file that failed to
# collect. Then:
# - accepted and unwarned => collected, unless the module failed to collect
#   (a loud error in the owning repository's own run);
# - collected => accepted, except the deliberate refusals below;
# - accepted and (not collected or not run) => warned;
# - a planted visible skip => warned.
REFUSED_BUT_COLLECTED = {
    "testc": "collected by pytest's default python_functions, but not by repositories that set test_*",
    "@identity": "a decorator outside the allowed list could replace the function",
    "@staticmethod": "a decorator outside the allowed list",
    "(Base)": "a base class can add __init__, __test__ or marks the file does not show",
    "(unittest.TestCase)": "unittest collection follows other rules",
    "__test__ = True": "any __test__ binding is refused",
    "async": "an async test fails without a plugin; refused rather than modelled",
    "__test__ store": "any store to __test__ (attribute, subscript, setattr) refuses the whole module",
    "generator test": "a generator test anywhere refuses the whole module, even one rebound later",
    "def _annotated(a: globals().__setitem__('__test__', False)):\n    pass": (
        "annotations run at import before Python 3.14 (here they turn collection off); from 3.14 they are lazy"
    ),
}
# Features that can stop a collected test running, all visible in the file.
VISIBLE_SKIPS = {
    "@pytest.mark.skip", "@pytest.mark.skip(reason='r')", "@pytest.mark.skipif(True, reason='r')",
    "@pytest.mark.skipif(False, reason='r')", "@pytest.mark.xfail", "@pytest.mark.parametrize('{arg}', [])",
    "@pytest.mark.parametrize('{arg}', EMPTY)", "@pytest.mark.parametrize('{arg}', [pytest.param(1, marks=pytest.mark.skip)])",
    "@settings(phases=())\n@given({arg}=st.integers())", "@pytest.mark.usefixtures('skipper')",
    "skip-body", "importorskip-body", "if-skip-body", "skipper-arg",
    "pytestmark = pytest.mark.skip", "pytestmark = pytest.mark.skipif(False, reason='r')",
    "def setup_method(self):\n        pytest.skip('no data')", "def setup_module():\n    pytest.skip('no data')",
    "@pytest.fixture(autouse=True)\ndef auto_skip():\n    pytest.skip('no data')", "pytest.mark.skip({})",
}
# (source, can carry other decorators). {arg} is the function's own argument name.
FUNCTION_DECORATORS = [
    ("@pytest.mark.slow", True),
    ("@pytest.mark.parametrize('{arg}', [1, 2])", True),
    ("@pytest.mark.parametrize('{arg}', [])", True),
    ("@pytest.mark.parametrize('{arg}', EMPTY)", True),
    ("@pytest.mark.parametrize('{arg}', [pytest.param(1, marks=pytest.mark.skip)])", True),
    ("@pytest.mark.parametrize('{arg}', [pytest.param(1, marks=pytest.mark.slow)])", True),
    ("@given({arg}=st.integers())", True),
    ("@settings(phases=())\n@given({arg}=st.integers())", True),
    ("@pytest.mark.usefixtures('skipper')", True),
    ("@pytest.mark.skip", True),
    ("@pytest.mark.skip(reason='r')", True),
    ("@pytest.mark.skipif(True, reason='r')", True),
    ("@pytest.mark.skipif(False, reason='r')", True),
    ("@pytest.mark.xfail", True),
    ("@identity", True),
    ("@noop", False),  # a mark or given over None is a loud error
    ("@pytest.fixture", False),  # marks on a fixture are a loud collection error
    ("@pytest.fixture(params=[1])", False),
]
METHOD_ONLY = [("@staticmethod", False), ("@property", False)]
BODIES = {
    "pass": "    pass",
    "skip-body": "    pytest.skip('no data')",
    "importorskip-body": "    pytest.importorskip('no_such_module_xyz')",
    "if-skip-body": "    if True:\n        pytest.skip('no data')",
}
CLASS_DECORATORS = ["", "@pytest.mark.slow", "@pytest.mark.skip", "@pytest.mark.skipif(False, reason='r')", "@dataclasses.dataclass"]
CLASS_BASES = ["", "(object)", "(Base)", "(unittest.TestCase)"]
SCOPE_LINES = [
    "", "__test__ = False", "__test__ = True", "pytestmark = pytest.mark.skip", "pytestmark = [pytest.mark.slow]",
    "pytestmark = pytest.mark.skipif(False, reason='r')",
]
CLASS_LINES = SCOPE_LINES + [
    "def __init__(self):\n        pass", "def __new__(cls):\n        return object.__new__(cls)",
    "def setup_method(self):\n        pytest.skip('no data')",
]
MODULE_LINES = SCOPE_LINES + [
    "pytest.skip('later', allow_module_level=True)", "def setup_module():\n    pytest.skip('no data')",
    "@pytest.fixture(autouse=True)\ndef auto_skip():\n    pytest.skip('no data')",
    # Round-4 review: shapes that turn collection off in a file that shows everything.
    "if __name__ == '__main__':\n    pass\nelse:\n    pytest.skip('module', allow_module_level=True)",
    "class object:\n    __test__ = False",
    "def __getattr__(name):\n    if name == '__test__':\n        return False\n    raise AttributeError(name)",
    "def _annotated(a: globals().__setitem__('__test__', False)):\n    pass",
    # Final review of 6a21582e: a skip at import through a helper or an alias.
    "def _require_data():\n    pytest.skip('no data', allow_module_level=True)\n_require_data()",
    "skip_module = pytest.skip\nskip_module('no data', allow_module_level=True)",
    # Round-6 review: call forms the screen cannot follow; it warns on them.
    "from functools import partial\nskip_if_missing = partial(pytest.skip, 'no data', allow_module_level=True)\nskip_if_missing()",
    "skip_maybe = pytest.skip if EMPTY == [] else print\nskip_maybe('no data', allow_module_level=True)",
    "skip_annotated: object = pytest.skip\nskip_annotated('no data', allow_module_level=True)",
    "def _gate():\n    pytest.skip('no data', allow_module_level=True)\ngate = _gate\ngate()",
    "try:\n    def _gate_in_try():\n        pytest.skip('no data', allow_module_level=True)\nexcept Exception:\n    pass\n_gate_in_try()",
    # Project code called at import: one helper skips the module, one does nothing.
    "from helpers_mod import require_data\nrequire_data()",
    "from helpers_mod import prepare\nprepare()",
    # Statically unreachable: never runs.
    "def _never():\n    pytest.skip('no data', allow_module_level=True)\nif False:\n    _never()",
]
HELPERS_MOD = (
    "import pytest\n\n"
    "def require_data():\n    pytest.skip('no data', allow_module_level=True)\n\n"
    "def prepare():\n    return None\n"
)
HEADER = (
    "import dataclasses\nimport unittest\nimport pytest\nfrom hypothesis import given, settings\n"
    "import hypothesis.strategies as st\n\nEMPTY = []\n\n"
    "def identity(f):\n    return f\n\ndef noop(f):\n    return None\n\nclass Base:\n    pass\n\n"
    "@pytest.fixture\ndef skipper():\n    pytest.skip('no data')\n"
)


@st.composite
def generated_defs(draw, name, method, top_level):
    """One def, the features it uses that the resolver refuses on purpose, and
    the visible skips it carries."""

    pool = FUNCTION_DECORATORS + (METHOD_ONLY if method else [])
    chosen = draw(st.lists(st.sampled_from(range(len(pool))), max_size=2, unique=True))
    decorators = [pool[i] for i in chosen]
    if any(not combines for _, combines in decorators):
        decorators = [next(d for d in decorators if not d[1])]
    texts = [text.replace("{arg}", f"a{i}") for i, (text, _) in enumerate(decorators)]
    texts.sort(key=lambda text: text not in {"@staticmethod", "@property"})  # outermost
    args = [f"a{i}" for i, (text, _) in enumerate(decorators) if "{arg}" in text]
    features = {text for text, _ in decorators}
    fixture = any("fixture(" in text or text == "@pytest.fixture" for text in texts)
    if not fixture and draw(st.integers(0, 3)) == 0:
        args.append("skipper")
        features.add("skipper-arg")
    if method and "@staticmethod" not in texts:
        args.insert(0, "self")
    flavour = draw(st.sampled_from(["pass"] * 6 + sorted(set(BODIES) - {"pass"})))  # mostly clean, so unwarned runs are common
    if top_level and not texts and "skipper" not in args:
        flavour = draw(st.sampled_from([flavour, flavour, "generator", "async"]))
    body = "    yield 1" if flavour == "generator" else BODIES.get(flavour, "    pass")
    prefix = "async def" if flavour == "async" else "def"
    features.add(flavour)
    source = "".join(f"{text}\n" for text in texts) + f"{prefix} {name}({', '.join(args)}):\n{body}\n"
    refused = (features | {name}) & REFUSED_BUT_COLLECTED.keys()
    return source, refused, features & VISIBLE_SKIPS


@st.composite
def generated_modules(draw):
    """(source, {candidate name: (refused-on-purpose features, visible skips)})."""

    module_line = draw(st.sampled_from([""] * 4 + MODULE_LINES))
    parts = [HEADER, module_line + "\n" if module_line else ""]
    candidates: dict[str, tuple[set[str], set[str]]] = {}
    module_refused = {module_line} & REFUSED_BUT_COLLECTED.keys()
    module_skips = {module_line} & VISIBLE_SKIPS
    for name in draw(st.lists(st.sampled_from(["test_a", "test_b", "testc", "helper"]), min_size=1, max_size=3, unique=True)):
        source, refused, skips = draw(generated_defs(name, method=False, top_level=True))
        parts.append(source)
        candidates[name] = (refused | module_refused, skips | module_skips)
    for cls in draw(st.lists(st.sampled_from(["TestK", "Helper"]), max_size=2, unique=True)):
        decorator, base, line = (
            draw(st.sampled_from([""] * 2 + CLASS_DECORATORS)),
            draw(st.sampled_from([""] * 2 + CLASS_BASES)),
            draw(st.sampled_from([""] * 3 + CLASS_LINES)),
        )
        class_features = {decorator, base, line}
        lines = [decorator] if decorator else []
        lines.append(f"class {cls}{base}:")
        if line:
            lines.append(f"    {line}")
        for name in draw(st.lists(st.sampled_from(["test_a", "test_b", "testc", "helper"]), min_size=1, max_size=2, unique=True)):
            source, refused, skips = draw(generated_defs(name, method=True, top_level=False))
            lines.extend(f"    {row}" for row in source.splitlines())
            candidates[f"{cls}::{name}"] = (
                refused | (class_features & REFUSED_BUT_COLLECTED.keys()) | module_refused,
                skips | (class_features & VISIBLE_SKIPS) | module_skips,
            )
        parts.append("\n".join(lines) + "\n")
    names = [name for name in candidates if "::" not in name] + sorted({name.split("::")[0] for name in candidates if "::" in name})
    tail = draw(st.sampled_from(["", "", "", "{} = None", "del {}", "{}.__test__ = False", "[{}][0].__test__ = False", "pytest.mark.skip({})"]))
    if tail:
        target = draw(st.sampled_from(names))
        parts.append(tail.format(target) + "\n")
        if tail in VISIBLE_SKIPS:
            for name, (refused, skips) in candidates.items():
                if name == target or name.startswith(f"{target}::"):
                    skips.add(tail)
        if "__test__" in tail:
            for refused, _ in candidates.values():
                refused.add("__test__ store")
    if "    yield 1" in "".join(parts):
        for refused, _ in candidates.values():
            refused.add("generator test")
    for decoy in ("test_missing", "TestK::test_missing"):
        candidates.setdefault(decoy, (set(), set()))
    return "\n".join(parts), candidates


ORACLE_PLUGIN = """
import json, os
ITEMS, PASSED, BROKEN = {}, set(), set()
def pytest_collectreport(report):
    if report.failed:
        BROKEN.add(report.nodeid.split("::")[0])
def pytest_collection_modifyitems(items):
    for item in items:
        cls = getattr(item, "cls", None)
        ITEMS[item.nodeid] = [item.path.name, cls.__name__ if cls else None, getattr(item, "originalname", item.name)]
def pytest_runtest_logreport(report):
    if report.when == "call" and report.outcome == "passed" and not hasattr(report, "wasxfail"):
        PASSED.add(report.nodeid)
def pytest_sessionfinish(session):
    with open(os.environ["ORACLE_OUT"], "w") as f:
        json.dump({"collected": list(ITEMS.values()), "ran": [ITEMS[n] for n in sorted(PASSED) if n in ITEMS],
                   "broken": sorted(BROKEN)}, f)
"""


def _pytest_outcomes(folder: Path, files: list[str]) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[str]]:
    """(file, test) pairs collected, pairs whose call passed, and files that
    failed to collect."""

    (folder / "_oracle.py").write_text(ORACLE_PLUGIN)
    (folder / "helpers_mod.py").write_text(HELPERS_MOD)
    (folder / "pytest.ini").write_text("[pytest]\n")
    out = folder / "oracle.json"
    env = {**os.environ, "PYTHONPATH": str(folder), "ORACLE_OUT": str(out), "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "_oracle", "-W", "ignore",
         "--continue-on-collection-errors", "--import-mode=importlib", "-c", "pytest.ini", "--rootdir", ".", *files],
        cwd=folder, env=env, capture_output=True, check=False, timeout=300,
    )
    result = json.loads(out.read_text())

    def pairs(rows):
        return {(path, f"{cls}::{name}" if cls else name) for path, cls, name in rows}

    return pairs(result["collected"]), pairs(result["ran"]), set(result["broken"])


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large])
@given(modules=st.lists(generated_modules(), min_size=1, max_size=3))
def test_resolver_accepts_exactly_what_pytest_collects(tmp_path_factory, modules):
    folder = tmp_path_factory.mktemp("collect")
    files = []
    for index, (source, _) in enumerate(modules):
        files.append(f"test_m{index}.py")
        (folder / files[-1]).write_text(source)
    collected, ran, broken = _pytest_outcomes(folder, files)
    for path, (source, candidates) in zip(files, modules):
        for name, (refused, skips) in candidates.items():
            accepted = sc.test_exists(path, name, source.encode())
            warned = bool(sc.python_test_warnings(source, name))
            seen = (path, name) in collected
            # Accepted and unwarned implies collected, unless collection fails
            # loudly in the owning repository's run.
            assert seen or not accepted or warned or path in broken, f"accepted {name} unwarned, not collected:\n{source}"
            assert accepted or not seen or refused, f"refused {name}, which pytest collected:\n{source}"
            if accepted and path not in broken:
                # Accepted and (not collected or not run) implies warned; a
                # warning may also fire on a test that runs.
                assert warned or (path, name) in ran, f"{name} did not run, with no warning:\n{source}"
                assert warned or not skips, f"{name}: visible skips {sorted(skips)} but no warning:\n{source}"
