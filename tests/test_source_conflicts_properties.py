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
11. Differential: the syntactic test screen (`python_test_exists`) agrees
    with Python's own symbol table on whether a generated module defines each
    name at module level or in a module-level class, through if, try, with,
    loops, match and nested functions; and it never raises on any text or
    bytes (input CPython's parser cannot handle, too deep or too complex, is a
    failed screen; tests/test_source_conflicts.py has bounded cases). The screen decides nothing about collection; tools/pin_runner.py
    asks pytest (tests/test_pin_runner*.py).
12. References by id: a `follows`, `adopts` or `pins` value that is neither
    `reading` nor an alternative id of the entry is always reported, and
    reordering an entry's alternatives never changes whether it passes or
    which reading any reference names.
13. Conflict state: `resolution` present without `conflict: resolved`, or
    `conflict: resolved` without a `resolution` or `years.to`, is always
    reported.
14. Kinds: a `text_kinds` override is accepted exactly when its kind is one
    its path's document class allows; a document class without a
    DOCUMENT_CLASS_KINDS row is always reported.
15. Lag: verify reports an implementation as lagging exactly when what it
    follows differs from what it adopts (`reading` when it names none).
16. Shipped commits: verify reports an external commit (snapshot,
    implementation or test) exactly when it is not reachable from its
    repository's default branch.

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

# Free text without digits, '@', '/' or '~', so it never trips hygiene by
# accident, and without a money spelling ("usd" is one), so it never trips the
# money rule.
prose = st.text(alphabet=string.ascii_letters + " ,.;:'()-", min_size=1, max_size=40).filter(
    lambda s: s.strip() and not sc._unsupported_money(s)
)
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
    st.sampled_from(["statute", "regulation", "guidance", "manual", "form", "policy"]),
    st.lists(st.from_regex(r"[a-z0-9][a-z0-9.-]{0,5}", fullmatch=True), min_size=1, max_size=4),
).filter(lambda path: not any(pattern.search(path) for pattern, _ in sc.HYGIENE_PATTERNS))  # e.g. us/statute/d000
urls = st.builds(lambda host, path: f"https://{host}.org/{path}", st.from_regex(r"[a-z]{3,8}", fullmatch=True), kebab)
texts = st.lists(st.fixed_dictionaries({"corpus_citation_path": corpus_paths, "excerpt": prose}), max_size=3)
pending = st.lists(
    st.fixed_dictionaries(
        {
            "citation": prose,
            "kind": st.sampled_from(sc.SOURCE_KINDS),
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
alternative_ids = kebab.filter(lambda s: s != sc.ADOPTED_READING)


@st.composite
def alternatives(draw):
    ids = draw(st.lists(alternative_ids, min_size=1, max_size=3, unique=True))
    return [
        {"id": alternative_id, "reading": draw(prose), **({"effect": draw(prose)} if draw(st.booleans()) else {})}
        for alternative_id in ids
    ]


years_values = st.one_of(
    st.builds(lambda start, span, kind: {"from": start, "to": start + span, "type": kind},
              st.integers(1990, 2030), st.integers(0, 10), st.sampled_from([t for t in sc.YEAR_TYPES if t != "program"])),
    st.builds(lambda start, note: {"from": start, "type": "program", "note": note}, st.integers(1990, 2030), prose),
)
unpublished = st.lists(
    st.fixed_dictionaries({"kind": st.sampled_from(sc.SOURCE_KINDS), "description": prose, "date": dates.map(str)}),
    min_size=1,
    max_size=2,
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
    alts = draw(alternatives())
    ids = [alt["id"] for alt in alts]
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
    if draw(st.booleans()):
        value["years"] = draw(years_values)
    if draw(st.booleans()) and "to" in value.get("years", {}):
        value["conflict"] = "resolved"
        value["resolution"] = {"by": draw(st.sampled_from(sc.RESOLVERS)), "effective": str(draw(dates)), "record": draw(urls)}
    if draw(st.booleans()):
        value["unpublished_sources"] = draw(unpublished)
    overridable = [
        text["corpus_citation_path"]
        for text in body_texts
        if len(sc.DOCUMENT_CLASS_KINDS[sc.document_class(text["corpus_citation_path"])]) > 1
    ]
    if overridable and draw(st.booleans()):
        path = draw(st.sampled_from(overridable))
        value["text_kinds"] = {path: draw(st.sampled_from(sc.DOCUMENT_CLASS_KINDS[sc.document_class(path)]))}
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
                "follows": draw(st.sampled_from(["reading", *ids])),
                "cites_entry": draw(st.booleans()),
            }
        ]
        if draw(st.booleans()):
            value["implementations"][0]["adopts"] = draw(st.sampled_from(["reading", *ids]))
            value["implementations"][0]["basis"] = draw(prose)
            if draw(st.booleans()):
                value["implementations"][0]["ruling"] = draw(records)
                if draw(st.booleans()):
                    value["implementations"][0]["ruling"]["principle"] = draw(st.sampled_from(sc.PRINCIPLES))
        value["tests"] = [
            {"repo": "PolicyEngine/microcosm-dynamics", "commit": commit, "path": "tests/test_x.py", "name": "test_default", "pins": ["reading"]}
        ] + [
            {"repo": "PolicyEngine/microcosm-dynamics", "commit": commit, "path": "tests/test_x.py", "name": f"test_alt_{i}", "pins": [alternative_id]}
            for i, alternative_id in enumerate(ids)
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
    drifted[field] = {"texts": [], "alternatives": [{"id": "different", "reading": "different"}]}.get(field, "different")
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
    probe = {
        "id": "probe",
        "question": "q",
        "texts": [{"corpus_citation_path": "us/statute/1/1", "excerpt": "x"}],
        "reading": "r",
        "alternatives": [{"id": "other", "reading": "a"}],
        "basis": "b",
        "status": "open",
        "affects": ["rule_a"],
    }
    if validator({"module": {"interpretations": [probe]}, "rules": [{"name": "rule_a"}]}, source_texts=None):
        pytest.skip(
            "this axiom-encode does not accept alternatives[].id yet; the required id lands in "
            "TheAxiomFoundation/axiom-encode#1835 and TheAxiomFoundation/axiom-rules-engine#247"
        )
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
    lambda item: item["alternatives"][0].pop("id"),
    lambda item: item["alternatives"][0].__setitem__("id", "Not_Kebab"),
    lambda item: item["alternatives"].append(dict(item["alternatives"][0])),
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


# 11. Differential: the syntactic test screen against Python's own symbol
# table. A def is defined at module level, or in a module-level class, exactly
# when symtable gives the module (or that class) a child function scope with
# that name; compound statements (if, try, with, loops, match) open no scope.
# The screen says nothing about collection: tools/pin_runner.py asks pytest.
DEF_NAMES = ["test_a", "test_b"]
CLASS_NAMES = ["TestC", "TestD"]
CANDIDATES = DEF_NAMES + [f"{cls}::{name}" for cls in CLASS_NAMES for name in DEF_NAMES]


def _indent(lines: list[str]) -> list[str]:
    return ["    " + line for line in lines]


@st.composite
def blocks(draw, depth: int = 0) -> list[str]:
    """A non-empty block of statements, as source lines."""

    lines: list[str] = []
    for _ in range(draw(st.integers(1, 3))):
        kinds = ["pass", "def", "assign"] + (["class", "if", "try", "with", "for", "while", "match", "nested"] if depth < 3 else [])
        kind = draw(st.sampled_from(kinds))
        name = draw(st.sampled_from(DEF_NAMES))
        if kind == "pass":
            lines.append("pass")
        elif kind == "assign":
            lines.append(f"{name} = lambda: None")
        elif kind == "def":
            lines += [f"{draw(st.sampled_from(['def', 'async def']))} {name}(self=None):", *_indent(["pass"])]
        elif kind == "nested":  # a def inside a function is not module level
            lines += [f"def outer_{depth}():", *_indent(draw(blocks(depth + 1)))]
        elif kind == "class":
            lines += [f"class {draw(st.sampled_from(CLASS_NAMES))}:", *_indent(draw(blocks(depth + 1)))]
        elif kind == "if":
            lines += ["if X:", *_indent(draw(blocks(depth + 1)))]
            if draw(st.booleans()):
                lines += ["else:", *_indent(draw(blocks(depth + 1)))]
        elif kind == "try":
            lines += ["try:", *_indent(draw(blocks(depth + 1))), "except ImportError:", *_indent(draw(blocks(depth + 1)))]
            if draw(st.booleans()):
                lines += ["finally:", *_indent(draw(blocks(depth + 1)))]
        elif kind == "with":
            lines += ["with open('x') as handle:", *_indent(draw(blocks(depth + 1)))]
        elif kind == "for":
            lines += ["for item in []:", *_indent(draw(blocks(depth + 1)))]
        elif kind == "while":
            lines += ["while False:", *_indent(draw(blocks(depth + 1)))]
        else:
            lines += ["match X:", *_indent(["case 1:", *_indent(draw(blocks(depth + 1)))])]
    return lines


def _symtable_defines(source: str, name: str) -> bool:
    import symtable

    scope, _, function = name.rpartition("::")
    tables = symtable.symtable(source, "test_generated.py", "exec").get_children()
    if scope:
        tables = [child for table in tables if table.get_type() == "class" and table.get_name() == scope for child in table.get_children()]
    return any(table.get_type() == "function" and table.get_name() == function for table in tables)


@SETTINGS
@given(lines=blocks())
def test_screen_matches_the_symbol_table(lines):
    source = "\n".join(lines) + "\n"
    for name in CANDIDATES:
        assert sc.python_test_exists(source, name) == _symtable_defines(source, name), (name, source)
        assert sc.python_test_exists(source.encode(), name) == _symtable_defines(source, name), (name, source)


# 12. References by id
@SETTINGS
@given(value=entries(), ref=st.text(max_size=20), field=st.sampled_from(["follows", "adopts", "pins"]))
def test_unknown_reading_references_fail(tmp_path, value, ref, field):
    value = copy.deepcopy(value)
    assume(ref != sc.ADOPTED_READING and ref not in sc.alternative_ids(value["alternatives"]))
    commit = "c" * 40
    implementation = {"repo": "PolicyEngine/policyengine-us", "commit": commit, "path": "x.py", "follows": "reading", "cites_entry": False}
    value["implementations"] = [implementation]
    value["tests"] = [{"repo": "PolicyEngine/policyengine-us", "commit": commit, "path": "t.py", "name": "test_x", "pins": ["reading"]}]
    if field == "pins":
        value["tests"][0]["pins"] = ["reading", ref]
    else:
        implementation[field] = ref
        implementation["basis"] = "why"
    issues = sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False)
    assert any("which is neither `reading` nor the id" in issue or "must be `reading` or the id" in issue for issue in issues)


def _resolved_reading(entry, ref):
    if ref == sc.ADOPTED_READING:
        return entry["reading"]
    return next(alt["reading"] for alt in entry["alternatives"] if alt["id"] == ref)


@SETTINGS
@given(value=entries(), data=st.data())
def test_reordering_alternatives_keeps_validity_and_picks(tmp_path, value, data):
    shuffled = copy.deepcopy(value)
    shuffled["alternatives"] = data.draw(st.permutations(value["alternatives"]))
    ctx = repo(tmp_path)
    check = lambda e: sc.check_register({"version": 1, "entries": [e]}, ctx, scan_modules=False)  # noqa: E731
    assert check(shuffled) == check(value) == []
    for before, after in zip(value.get("implementations", []), shuffled.get("implementations", [])):
        for key in ("follows", "adopts"):
            if key in before:
                assert _resolved_reading(value, before[key]) == _resolved_reading(shuffled, after[key])


# 13. Conflict state
@SETTINGS
@given(value=entries(), conflict=st.sampled_from([None, "live", "resolved"]), with_resolution=st.booleans(), with_to=st.booleans())
def test_conflict_state_rules(tmp_path, value, conflict, with_resolution, with_to):
    value = copy.deepcopy(value)
    for key in ("conflict", "resolution"):
        value.pop(key, None)
    value["years"] = {"from": 2020, "type": "tax", **({"to": 2023} if with_to else {})}
    if conflict is not None:
        value["conflict"] = conflict
    if with_resolution:
        value["resolution"] = {"by": "agency", "effective": "2024-01-01", "record": "https://example.org/r"}
    issues = sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False)
    resolved = conflict == "resolved"
    expected_wrong = (resolved and not with_resolution) or (not resolved and with_resolution) or (resolved and not with_to)
    assert bool(issues) == expected_wrong, issues
    if not resolved and with_resolution:
        assert any("resolution must be absent while conflict is `live`" in issue for issue in issues)


# 14. Kinds
@SETTINGS
@given(value=entries(), document_class=st.sampled_from(sorted(sc.DOCUMENT_CLASS_KINDS)), kind=st.sampled_from(sc.SOURCE_KINDS))
def test_text_kind_overrides_match_the_document_class(tmp_path, value, document_class, kind):
    value = copy.deepcopy(value)
    path = f"us/{document_class}/a/1"
    value["texts"] = [{"corpus_citation_path": path, "excerpt": "words"}]
    value["text_kinds"] = {path: kind}
    issues = sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False)
    allowed = kind in sc.DOCUMENT_CLASS_KINDS[document_class]
    assert (not any("text_kinds" in issue for issue in issues)) == allowed
    assert sc.text_kind(value, path) == kind


@SETTINGS
@given(value=entries(), document_class=st.from_regex(r"[a-z][a-z-]{1,12}", fullmatch=True))
def test_unknown_document_classes_fail_closed(tmp_path, value, document_class):
    assume(document_class not in sc.DOCUMENT_CLASS_KINDS)
    value = copy.deepcopy(value)
    value.pop("text_kinds", None)
    value["texts"] = [{"corpus_citation_path": f"us/{document_class}/a/1", "excerpt": "words"}]
    issues = sc.check_register({"version": 1, "entries": [value]}, repo(tmp_path), scan_modules=False)
    assert any("has no DOCUMENT_CLASS_KINDS row" in issue for issue in issues)
    assert sc.text_kind(value, f"us/{document_class}/a/1") is None


# 15. Lag
@SETTINGS
@given(value=entries(), data=st.data())
def test_verify_lag_iff_follows_differs_from_adopts(value, data):
    value = copy.deepcopy(value)
    value["texts"] = []
    value.pop("pending_texts", None)
    refs = ["reading", *sc.alternative_ids(value["alternatives"])]
    follows = data.draw(st.sampled_from(refs))
    adopts = data.draw(st.one_of(st.none(), st.sampled_from(refs)))
    implementation = {"repo": "PolicyEngine/policyengine-us", "commit": "c" * 40, "path": "x.py", "follows": follows, "cites_entry": False}
    if adopts is not None:
        implementation["adopts"] = adopts
    value["implementations"] = [implementation]
    value["tests"] = []
    _, report = sc.verify_register(
        {"version": 1, "entries": [value]}, fetch=lambda repo, commit, path: b"", matches=lambda a, b: True, resolve=None
    )
    lagging = any(line.startswith("lags:") for line in report)
    assert lagging == (follows != (adopts or "reading"))


# 16. Shipped commits
@SETTINGS
@given(value=entries(), data=st.data())
def test_verify_flags_exactly_the_unshipped_commits(value, data):
    value = copy.deepcopy(value)
    value["texts"] = []
    external = []
    for item in value.get("pending_texts", []):
        external.append((item["snapshot"]["repo"], item["snapshot"]["commit"]))
    for key in ("implementations", "tests"):
        for item in value.get(key, []):
            if "repo" in item:
                external.append((item["repo"], item["commit"]))
    shipped = {pair: data.draw(st.booleans()) for pair in set(external)}
    issues, _ = sc.verify_register(
        {"version": 1, "entries": [value]},
        fetch=lambda repo, commit, path: b"",
        matches=lambda a, b: True,
        resolve=None,
        on_default_branch=lambda repo, commit: shipped[(repo, commit)],
    )
    flagged = [issue for issue in issues if "default branch; pin a commit" in issue]
    assert len(flagged) == sum(1 for pair in external if not shipped[pair])


@SETTINGS
@given(source=st.one_of(st.text(max_size=200), st.binary(max_size=200)), name=st.one_of(st.sampled_from(CANDIDATES), st.text(max_size=20)))
def test_screen_is_total(source, name):
    assert sc.python_test_exists(source, name) in (True, False)
