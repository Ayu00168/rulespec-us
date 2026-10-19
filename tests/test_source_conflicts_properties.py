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
11. Differential: for generated @pytest.mark.parametrize decorators, the
    test-reference resolver's case ids equal the ids pytest itself collects,
    wherever the resolver answers; it refuses (fails closed) otherwise.

Hypothesis is not installed in the shared validate workflow's pytest leg, so
these skip there; the source-conflicts workflow installs it and runs them.
"""

from __future__ import annotations

import copy
import importlib.util
import string
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
)
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
    if draw(st.booleans()):
        value["money_case"] = {
            "description": draw(prose),
            "amount": draw(st.from_regex(r"(0|[1-9][0-9]{0,4})(\.[0-9]{2})?", fullmatch=True)),
            "currency": "USD",
            "period": draw(st.sampled_from(sc.PERIODS)),
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
    issues = sc.check_register(payload, repo(tmp_path), scan_modules=False)
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
    issues = sc.check_register({"version": 1, "entries": [value, copy.deepcopy(value)]}, repo(tmp_path), scan_modules=False)
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


# 11. Differential: the test-reference resolver against pytest's own collection.
# Generated @pytest.mark.parametrize decorators (one or two stacked, names as a
# string or a tuple, literal values of every kind pytest names) are written to
# a file and collected by pytest in-process. Wherever the resolver answers, its
# ids must equal pytest's exactly; where it cannot tell, it refuses.
id_literals = st.one_of(
    st.integers(-5, 5),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.booleans(),
    st.none(),
    st.text(alphabet=string.ascii_letters + " .-_\n\té", min_size=1, max_size=4),
    st.binary(min_size=1, max_size=3),
    st.tuples(st.integers(0, 3)),
)


@st.composite
def parametrize_decorators(draw, argname):
    values = draw(st.lists(id_literals, min_size=1, max_size=4))
    explicit = draw(st.booleans())
    ids = ""
    if explicit:
        labels = draw(st.lists(st.one_of(st.none(), st.from_regex(r"[a-z]{1,5}", fullmatch=True)), min_size=len(values), max_size=len(values)))
        ids = f", ids={labels!r}"
    shape = draw(st.sampled_from(["name", "tuple", "trailing-comma"]))
    if shape == "tuple":
        return f"@pytest.mark.parametrize(({argname!r},), {[ (v,) for v in values ]!r}{ids})"
    if shape == "trailing-comma":
        return f"@pytest.mark.parametrize({argname + ','!r}, {[ (v,) for v in values ]!r}{ids})"
    return f"@pytest.mark.parametrize({argname!r}, {values!r}{ids})"


_COUNTER = __import__("itertools").count()


def _pytest_ids(path: Path) -> list[str]:
    collected: list[str] = []

    class Collector:
        def pytest_collection_modifyitems(self, items):
            collected.extend(item.name for item in items)

    # importlib mode, and a fresh file name per example, keep the nested
    # collections from reusing one another's modules.
    pytest.main(
        ["--collect-only", "-q", "-p", "no:cacheprovider", "-W", "ignore", "--import-mode=importlib", "--rootdir", str(path.parent), str(path)],
        plugins=[Collector()],
    )
    return [name.split("[", 1)[1][:-1] for name in collected if "[" in name]


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(first=parametrize_decorators("a"), second=st.one_of(st.none(), parametrize_decorators("b")))
def test_resolver_agrees_with_pytest_collection(tmp_path_factory, first, second, capsys):
    decorators = [first] + ([second] if second else [])
    params = ", ".join(["a"] + (["b"] if second else []))
    source = "import pytest\n" + "\n".join(decorators) + f"\ndef test_case({params}):\n    pass\n"
    path = tmp_path_factory.mktemp("collect") / f"test_generated_{next(_COUNTER)}.py"
    path.write_text(source)
    theirs = _pytest_ids(path)
    capsys.readouterr()
    tree = __import__("ast").parse(source)
    function = tree.body[-1]
    combined = [""]
    for decorator in reversed(function.decorator_list):
        ids = sc.parametrize_ids(decorator)
        if ids is None:
            combined = None
            break
        combined = [f"{left}-{right}" if left else right for left in combined for right in ids]
    if combined is not None:
        assert sorted(combined) == sorted(theirs), (source, combined, theirs)
        for case_id in combined:
            if "]" not in case_id and "\n" not in case_id and case_id:
                assert sc.python_test_exists(source, f"test_case[{case_id}]")
