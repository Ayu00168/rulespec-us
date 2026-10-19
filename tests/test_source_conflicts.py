"""Tests for tools/source_conflicts.py and the committed source-conflict register.

These run in the Repository Checks pytest leg, offline. The committed
known-source-conflicts.yaml must pass `check` exactly (schema, status rules,
hygiene, in-repo references, and the scan that every module interpretation
citing two or more provisions is registered). The rest pin each rule on small
fixture repositories. Network verification (excerpts against the pinned corpus
release and snapshots, external refs) is the source-conflicts workflow;
property-based and differential tests live in test_source_conflicts_properties.py.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "source_conflicts.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("source_conflicts", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["source_conflicts"] = module
    spec.loader.exec_module(module)
    return module


sc = _load_tool()
REPO = "TheAxiomFoundation/rulespec-us"
COMMIT = "a" * 40
SHA = "b" * 64


# ---------------------------------------------------------------------------
# The committed register


def test_committed_register_passes_check() -> None:
    payload = sc.load_register(REPO_ROOT / sc.REGISTER_FILENAME)
    issues = sc.check_register(payload, sc.RepoContext(root=REPO_ROOT, repo=REPO))
    assert issues == []


def test_committed_register_cli_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert sc.main(["check", "--root", str(REPO_ROOT)]) == 0
    assert "0 issue(s), 0 unregistered open interpretation(s)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Fixtures


def make_repo(tmp_path: Path) -> sc.RepoContext:
    (tmp_path / "us" / "statutes" / "42" / "403" / "a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "us-co" / "regulations").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(exist_ok=True)
    return sc.RepoContext(root=tmp_path, repo=REPO)


def entry(**overrides):
    base = {
        "id": "family-maximum-saving-clause",
        "jurisdiction": "us",
        "program": "social-security",
        "question": "Does the saving clause protect every later month, or only months after a PIA increase?",
        "texts": [
            {
                "corpus_citation_path": "us/statute/42/403/a/5",
                "excerpt": "for any such subsequent month",
            }
        ],
        "pending_texts": [
            {
                "citation": "POMS RS 00630.400",
                "kind": "manual",
                "url": "https://secure.ssa.gov/poms.nsf/lnx/0300630400",
                "snapshot": {
                    "repo": "PolicyEngine/microcosm-dynamics",
                    "commit": COMMIT,
                    "path": "tests/data/poms_rs_00630_400.html",
                    "sha256": SHA,
                },
                "excerpt": "Total family payment is less after the PIA increase",
            }
        ],
        "reading": "Protect only when a PIA increase itself lowers the family total.",
        "alternatives": [{"id": "every-later-month", "reading": "Protect every later month.", "effect": "More months refused."}],
        "basis": "SSA's operational chart.",
        "status": "ruled",
        "ruling": {"by": "Axiom Foundation", "date": "2026-10-12", "record": "https://github.com/x/y/pull/1"},
        "outcome": "computed",
        "implementations": [
            {
                "repo": "PolicyEngine/microcosm-dynamics",
                "commit": COMMIT,
                "path": "src/gross_benefits.py",
                "follows": "every-later-month",
                "cites_entry": False,
            }
        ],
        "tests": [
            {
                "repo": "PolicyEngine/microcosm-dynamics",
                "commit": COMMIT,
                "path": "tests/test_g.py",
                "name": "test__given_operational__then_pays",
                "pins": ["reading"],
            },
            {
                "repo": "PolicyEngine/microcosm-dynamics",
                "commit": COMMIT,
                "path": "tests/test_g.py",
                "name": "TestLiteral::test_refuses[child-ages-out]",
                "pins": ["every-later-month"],
            },
        ],
        "money_case": {"description": "A survivor family after a child ages out.", "amount": "21.00", "currency": "USD", "period": "month"},
    }
    base.update(overrides)
    return base


def check(ctx, *entries, scan=True):
    return sc.check_register({"version": 1, "entries": list(entries)}, ctx, scan_modules=scan)


def write_module(ctx, rel, interpretations, rules=("rule_a", "rule_b")):
    path = ctx.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "rulespec/v1",
        "module": {"summary": "x", "interpretations": interpretations},
        "rules": [{"name": name, "kind": "parameter"} for name in rules],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


# ---------------------------------------------------------------------------
# Top level


def test_valid_entry_has_no_issues(tmp_path):
    assert check(make_repo(tmp_path), entry()) == []


def test_top_level_shape(tmp_path):
    ctx = make_repo(tmp_path)
    assert sc.check_register([], ctx) != []
    issues = sc.check_register({"version": 2, "entries": [], "extra": 1}, ctx)
    assert any("version must be 1" in issue for issue in issues)
    assert any("unknown top-level key `extra`" in issue for issue in issues)
    assert any("entries must be a list" in issue for issue in sc.check_register({"version": 1}, ctx))


def test_duplicate_ids(tmp_path):
    issues = check(make_repo(tmp_path), entry(), entry())
    assert any("`us:family-maximum-saving-clause` duplicates entries[0]" in issue for issue in issues)


def test_the_same_id_in_two_jurisdictions_is_two_entries(tmp_path):
    # Global ids (<jurisdiction>:<id>) are the identity; aggregate checks them across repositories.
    assert check(make_repo(tmp_path), entry(), entry(jurisdiction="us-co")) == []


@pytest.mark.parametrize("bad", ["Has-Caps", "trailing-", "two--dashes", "under_score", ""])
def test_id_pattern(tmp_path, bad):
    issues = check(make_repo(tmp_path), entry(id=bad))
    assert any(".id must match" in issue or ".id" in issue for issue in issues)


def test_jurisdiction_must_be_a_directory_here(tmp_path):
    ctx = make_repo(tmp_path)
    assert check(ctx, entry(jurisdiction="us-co")) == []
    assert any("jurisdiction must be" in issue for issue in check(ctx, entry(jurisdiction="uk")))


def test_unknown_keys_rejected_at_every_level(tmp_path):
    ctx = make_repo(tmp_path)
    bad = entry(extra=1)
    bad["texts"][0]["note"] = "x"
    bad["alternatives"][0]["why"] = "x"
    bad["ruling"]["officer"] = "x"
    bad["pending_texts"][0]["snapshot"]["branch"] = "main"
    bad["money_case"]["fx"] = 1
    issues = check(ctx, bad)
    for key in ("extra", "note", "why", "officer", "branch", "fx"):
        assert any(f"unknown key `{key}`" in issue for issue in issues), key


def test_required_fields(tmp_path):
    ctx = make_repo(tmp_path)
    for key in sc.REQUIRED_ENTRY_KEYS:
        bad = entry()
        del bad[key]
        assert check(ctx, bad), key


# ---------------------------------------------------------------------------
# Interpretation core: the module.interpretations v1 rules


def test_status_open_forbids_ruling(tmp_path):
    ctx = make_repo(tmp_path)
    issues = check(ctx, entry(status="open"))
    assert any("ruling must be absent when status is `open`" in issue for issue in issues)
    opened = entry(status="open")
    del opened["ruling"]
    assert check(ctx, opened) == []


def test_status_ruled_requires_ruling_and_forbids_confirmation(tmp_path):
    ctx = make_repo(tmp_path)
    bad = entry()
    del bad["ruling"]
    assert any("ruling must be a mapping when status is `ruled`" in issue for issue in check(ctx, bad))
    confirmation = {"by": "Social Security Administration", "date": "2026-10-20", "excerpt": "Yes."}
    assert any("confirmation must be absent" in issue for issue in check(ctx, entry(confirmation=confirmation)))


def test_status_confirmed_and_contradicted(tmp_path):
    ctx = make_repo(tmp_path)
    confirmation = {"by": "Social Security Administration", "date": "2026-10-20", "excerpt": "Yes."}
    assert check(ctx, entry(status="confirmed", confirmation=confirmation)) == []
    missing = entry(status="confirmed")
    assert any("confirmation must be a mapping" in issue for issue in check(ctx, missing))
    contradicted = check(ctx, entry(status="contradicted", confirmation=confirmation))
    assert any("contradicted this reading" in issue for issue in contradicted)
    no_excerpt = entry(status="confirmed", confirmation={"by": "SSA", "date": "2026-10-20"})
    assert any("confirmation.excerpt" in issue for issue in check(ctx, no_excerpt))
    bad_record = entry(status="confirmed", confirmation={**confirmation, "record": "http://x"})
    assert any("confirmation.record must be an https URL" in issue for issue in check(ctx, bad_record))


@pytest.mark.parametrize("status", ["Ruled", "pending", "", None, 1])
def test_status_enum(tmp_path, status):
    assert any("status must be" in issue for issue in check(make_repo(tmp_path), entry(status=status)))


@pytest.mark.parametrize("date", ["2026-02-30", "2026-1-01", "12/10/2026", "", 20261012])
def test_ruling_date_must_be_real(tmp_path, date):
    bad = entry()
    bad["ruling"]["date"] = date
    assert any("ruling.date must be a real" in issue for issue in check(make_repo(tmp_path), bad))


def test_unquoted_yaml_date_is_accepted(tmp_path):
    payload = yaml.safe_load(yaml.safe_dump({"version": 1, "entries": [entry()]}).replace("'2026-10-12'", "2026-10-12"))
    assert sc.check_register(payload, make_repo(tmp_path)) == []


@pytest.mark.parametrize("record", ["http://github.com/x", "github.com/x", "https://", "https://exa mple.com", None])
def test_ruling_record_must_be_https(tmp_path, record):
    bad = entry()
    bad["ruling"]["record"] = record
    assert any("ruling.record must be an https URL" in issue for issue in check(make_repo(tmp_path), bad))


def test_alternatives_non_empty(tmp_path):
    assert any("alternatives must be a non-empty list" in issue for issue in check(make_repo(tmp_path), entry(alternatives=[])))


def test_blank_strings_rejected(tmp_path):
    ctx = make_repo(tmp_path)
    for field in ("question", "reading", "basis"):
        assert any(f".{field} must be a non-blank string" in issue for issue in check(ctx, entry(**{field: "  "}))), field


@pytest.mark.parametrize("path", ["US/statute/42/403", "us/statute", "/us/statute/42", "us/statute/42/", "us//statute/42"])
def test_corpus_citation_path_canonical(tmp_path, path):
    bad = entry()
    bad["texts"][0]["corpus_citation_path"] = path
    assert any("canonical corpus citation path" in issue for issue in check(make_repo(tmp_path), bad))


def test_needs_a_provision(tmp_path):
    bad = entry(texts=[])
    del bad["pending_texts"]
    assert any("at least one provision" in issue for issue in check(make_repo(tmp_path), bad))
    only_pending = entry(texts=[])
    assert check(make_repo(tmp_path), only_pending) == []


def test_pending_text_snapshot_shape(tmp_path):
    ctx = make_repo(tmp_path)
    bad = entry()
    bad["pending_texts"][0]["snapshot"] = {"repo": "nope", "commit": "abc", "path": "../x", "sha256": "z"}
    bad["pending_texts"][0]["url"] = "ftp://x"
    issues = check(ctx, bad)
    for fragment in ("repo must be owner/name", "full lowercase commit", "relative path", "sha256 digest", "url must be an https URL"):
        assert any(fragment in issue for issue in issues), fragment


# ---------------------------------------------------------------------------
# Register-only fields


def test_outcome_enum(tmp_path):
    assert any("outcome must be computed or refused" in issue for issue in check(make_repo(tmp_path), entry(outcome="default")))


def test_reading_references(tmp_path):
    ctx = make_repo(tmp_path)
    bad = entry()
    bad["tests"][1]["pins"] = ["no-such-alternative"]
    bad["implementations"][0]["follows"] = "literal"
    issues = check(ctx, bad)
    assert any("pins[0] names `no-such-alternative`, which is neither `reading` nor the id" in issue for issue in issues)
    assert any("follows names `literal`, which is neither" in issue for issue in issues)
    blank = entry()
    blank["implementations"][0]["follows"] = ""
    assert any("follows must be `reading` or the id of one of this entry's alternatives" in issue for issue in check(ctx, blank))


@pytest.mark.parametrize("ref", ["alternatives[0]", "alternatives[1]", "0"])
def test_positional_references_are_rejected(tmp_path, ref):
    # References name alternatives by id: a position would silently move to
    # another alternative when one is inserted or reordered.
    bad = entry()
    bad["implementations"][0]["follows"] = ref
    bad["tests"][1]["pins"] = [ref]
    issues = check(make_repo(tmp_path), bad)
    assert any(f"follows names `{ref}`" in issue for issue in issues)
    assert any(f"pins[0] names `{ref}`" in issue for issue in issues)


def test_reordering_alternatives_keeps_every_reference(tmp_path):
    ctx = make_repo(tmp_path)
    value = entry()
    value["alternatives"].insert(0, {"id": "inserted-first", "reading": "Another reading."})
    assert check(ctx, value) == []
    value["alternatives"].reverse()
    assert check(ctx, value) == []
    assert value["implementations"][0]["follows"] == "every-later-month"


@pytest.mark.parametrize(
    ("alternatives", "fragment"),
    [
        ([{"reading": "No id."}], "alternatives[0].id is required"),
        ([{"id": "Not_Kebab", "reading": "x"}], "alternatives[0].id must match"),
        ([{"id": "reading", "reading": "x"}], "must not be `reading`"),
        ([{"id": "same", "reading": "x"}, {"id": "same", "reading": "y"}], "alternatives[1].id `same` repeats"),
        ([{"id": 3, "reading": "x"}], "alternatives[0].id must match"),
    ],
)
def test_alternative_ids(tmp_path, alternatives, fragment):
    bad = entry(alternatives=alternatives, implementations=[], tests=[])
    del bad["implementations"], bad["tests"]
    assert any(fragment in issue for issue in check(make_repo(tmp_path), bad)), fragment


def test_consumer_adopts_its_own_reading(tmp_path):
    ctx = make_repo(tmp_path)
    value = entry()
    implementation = value["implementations"][0]
    implementation["adopts"] = "every-later-month"
    implementation["basis"] = "This consumer follows the statute when it conflicts with an agency manual."
    implementation["ruling"] = {"by": "PolicyEngine", "date": "2026-10-19", "record": "https://github.com/x/y/pull/2"}
    assert check(ctx, value) == []
    unknown = copy.deepcopy(value)
    unknown["implementations"][0]["adopts"] = "statute"
    assert any("adopts names `statute`, which is neither" in issue for issue in check(ctx, unknown))
    no_basis = copy.deepcopy(value)
    del no_basis["implementations"][0]["basis"]
    assert any("basis is required with adopts or ruling" in issue for issue in check(ctx, no_basis))
    bad_ruling = copy.deepcopy(value)
    bad_ruling["implementations"][0]["ruling"]["record"] = "http://example.com"
    assert any("implementations[0].ruling.record must be an https URL" in issue for issue in check(ctx, bad_ruling))
    blank_basis = copy.deepcopy(value)
    blank_basis["implementations"][0]["basis"] = " "
    assert any("implementations[0].basis must be a non-blank string" in issue for issue in check(ctx, blank_basis))


def test_every_followed_reading_is_pinned(tmp_path):
    ctx = make_repo(tmp_path)
    bad = entry()
    bad["tests"] = [bad["tests"][0]]  # pins reading only; the implementation follows every-later-month
    assert any("follows every-later-month, which no test pins" in issue for issue in check(ctx, bad))
    both = entry()
    both["tests"] = [{**both["tests"][0], "pins": ["reading", "every-later-month"]}]
    assert check(ctx, both) == []
    no_adopter = entry(tests=[entry()["tests"][1]])
    del no_adopter["implementations"]
    assert check(ctx, no_adopter) == []


def test_pins_shape(tmp_path):
    ctx = make_repo(tmp_path)
    for pins, fragment in (("reading", "non-empty list"), ([], "non-empty list"), (["reading", "reading"], "repeats a reading")):
        bad = entry()
        bad["tests"][0]["pins"] = pins
        assert any(fragment in issue for issue in check(ctx, bad)), pins


def test_module_adopter_requires_a_test_pinning_the_reading(tmp_path):
    ctx = make_repo(tmp_path)
    value = adopted_entry()
    del value["implementations"]
    value["tests"] = [value["tests"][1]]
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["rule_a"])])
    assert any("adopted by a module, so at least one test must pin `reading`" in issue for issue in check(ctx, value))


def test_external_refs_need_commits_and_not_this_repo(tmp_path):
    ctx = make_repo(tmp_path)
    bad = entry()
    bad["tests"][0]["commit"] = "main"
    bad["implementations"][0]["repo"] = REPO
    issues = check(ctx, bad)
    assert any("tests[0].commit must be a full lowercase commit SHA" in issue for issue in issues)
    assert any("names this repository" in issue for issue in issues)
    missing_repo = entry()
    del missing_repo["implementations"][0]["repo"]
    assert any("goes in adopted_by" in issue for issue in check(ctx, missing_repo))


def test_cites_entry_must_be_boolean(tmp_path):
    bad = entry()
    bad["implementations"][0]["cites_entry"] = "no"
    assert any("cites_entry must be true or false" in issue for issue in check(make_repo(tmp_path), bad))


def test_in_repo_tests_must_exist(tmp_path):
    ctx = make_repo(tmp_path)
    (ctx.root / "us" / "statutes" / "42" / "403" / "a" / "5.test.yaml").write_text(
        yaml.safe_dump([{"name": "saving_clause_case", "period": "2026-01", "input": {}, "output": {}}])
    )
    (ctx.root / "tests" / "test_local.py").write_text("class TestX:\n    def test_y(self):\n        pass\n")
    good = entry()
    good["tests"] = [
        {"path": "us/statutes/42/403/a/5.test.yaml", "name": "saving_clause_case", "pins": ["reading"]},
        {"path": "tests/test_local.py", "name": "TestX::test_y", "pins": ["every-later-month"]},
    ]
    assert check(ctx, good) == []
    bad = copy.deepcopy(good)
    bad["tests"][0]["name"] = "missing_case"
    bad["tests"][1]["path"] = "tests/test_absent.py"
    issues = check(ctx, bad)
    assert any("missing_case is not defined" in issue for issue in issues)
    assert any("tests/test_absent.py does not exist" in issue for issue in issues)
    local_commit = copy.deepcopy(good)
    local_commit["tests"][0]["commit"] = COMMIT
    assert any("commit is only for a file in another repository" in issue for issue in check(ctx, local_commit))


def test_test_path_and_name_shapes(tmp_path):
    bad = entry()
    bad["tests"][0]["path"] = "tests/test_g.js"
    bad["tests"][1]["name"] = "not a name"
    issues = check(make_repo(tmp_path), bad)
    assert any("relative .py or .yaml path" in issue for issue in issues)
    assert any("must be a test function" in issue for issue in issues)
    local = entry()
    local["tests"] = [{"path": "tests/cases.yaml", "name": "a case", "pins": ["reading"]}]
    assert any("relative .py or .test.yaml path" in issue for issue in check(make_repo(tmp_path), local))


def test_external_yaml_test_cases(tmp_path):
    # Another repository's YAML policy tests (PolicyEngine-US) are pinned by
    # repo, commit, path and case name; the case name is free text.
    ctx = make_repo(tmp_path)
    value = entry()
    value["tests"][1] = {
        "repo": "PolicyEngine/policyengine-us",
        "commit": COMMIT,
        "path": "policyengine_us/tests/policy/baseline/gov/states/hi/tax/income/claim_of_right.yaml",
        "name": "Hawaii, repayment over $3,000, statute reading",
        "pins": ["every-later-month"],
    }
    assert check(ctx, value) == []
    for name in ("", " padded", "two\nlines", 7):
        bad = copy.deepcopy(value)
        bad["tests"][1]["name"] = name
        assert any("must be a YAML test case's name, on one line" in issue for issue in check(ctx, bad)), name
    assert sc.test_exists("cases.yaml", "Hawaii case", "- name: Hawaii case\n  input: {}\n")
    assert not sc.test_exists("cases.yaml", "Other case", "- name: Hawaii case\n")


def test_repeated_test_reference(tmp_path):
    bad = entry()
    bad["tests"].append(dict(bad["tests"][0]))
    assert any("repeats tests/test_g.py::test__given_operational__then_pays" in issue for issue in check(make_repo(tmp_path), bad))


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("amount", 21, "decimal string"),
        ("amount", "-3", "decimal string"),
        ("amount", "1,000", "decimal string"),
        ("currency", "usd", "ISO 4217"),
        ("period", "fortnight", "period must be one of"),
    ],
)
def test_money_case(tmp_path, field, value, fragment):
    bad = entry()
    bad["money_case"][field] = value
    assert any(fragment in issue for issue in check(make_repo(tmp_path), bad))


# ---------------------------------------------------------------------------
# Source kinds, years, conflict state, unpublished sources

# Every document class the corpus used when this was written (second segment
# of data/corpus/provisions/<jurisdiction>/<class>/ in axiom-corpus). A class
# missing from DOCUMENT_CLASS_KINDS fails closed, so a new one must get a row.
CORPUS_DOCUMENT_CLASSES = ("statute", "regulation", "rulemaking", "form", "guidance", "manual", "policy", "district-plan")


def test_every_corpus_document_class_has_a_kind_row():
    assert set(CORPUS_DOCUMENT_CLASSES) <= set(sc.DOCUMENT_CLASS_KINDS)
    for document_class, kinds in sc.DOCUMENT_CLASS_KINDS.items():
        assert kinds, document_class
        assert set(kinds) <= set(sc.SOURCE_KINDS), document_class


def test_text_kind_is_derived_from_the_document_class(tmp_path):
    value = entry()
    assert sc.text_kind(value, "us/statute/42/403/a/5") == "statute"
    assert sc.text_kind(value, "us-hi/form/n-11/2024/instructions") == "form"
    assert sc.text_kind(value, "us/unknown-class/x") is None
    value["text_kinds"] = {"us/statute/42/403/a/5": "statute"}
    assert sc.text_kind(value, "us/statute/42/403/a/5") == "statute"


def test_unknown_document_class_fails_closed(tmp_path):
    bad = entry(texts=[{"corpus_citation_path": "us/opinion/2026/1", "excerpt": "x"}])
    assert any("document class `opinion` has no DOCUMENT_CLASS_KINDS row" in issue for issue in check(make_repo(tmp_path), bad))


def test_text_kinds_override(tmp_path):
    ctx = make_repo(tmp_path)
    form_text = {"corpus_citation_path": "us-co/form/dr-0347/2025/line-1", "excerpt": "a qualifying child"}
    good = entry(texts=[entry()["texts"][0], form_text], text_kinds={"us-co/form/dr-0347/2025/line-1": "instructions"})
    assert check(ctx, good) == []
    assert sc.text_kind(good, "us-co/form/dr-0347/2025/line-1") == "instructions"
    for overrides, fragment in (
        ({"us/statute/42/403/a/5": "instructions"}, "a `statute` path can only be statute"),
        ({"us-co/form/dr-0347/2025/line-1": "speech"}, "must be one of statute"),
        ({"us/statute/42/403/a/6": "statute"}, "not one of this entry's texts"),
        ({}, "non-empty mapping"),
        ("instructions", "non-empty mapping"),
    ):
        bad = copy.deepcopy(good)
        bad["text_kinds"] = overrides
        assert any(fragment in issue for issue in check(ctx, bad)), overrides


@pytest.mark.parametrize("kind", [None, "", "Manual", "speech"])
def test_pending_text_kind_is_a_closed_enum(tmp_path, kind):
    bad = entry()
    if kind is None:
        del bad["pending_texts"][0]["kind"]
    else:
        bad["pending_texts"][0]["kind"] = kind
    assert any("pending_texts[0].kind must be one of" in issue for issue in check(make_repo(tmp_path), bad))


@pytest.mark.parametrize(
    ("years", "fragment"),
    [
        ({"from": 2022, "to": 2025, "type": "tax"}, None),
        ({"from": 2024, "type": "tax"}, None),
        ({"from": 2026, "to": 2026, "type": "federal-fiscal"}, None),
        ({"from": 2025, "type": "program", "note": "LIHEAP program year, October to September"}, None),
        ({"from": 2025, "type": "program"}, "note is required when type is `program`"),
        ({"from": 2025, "to": 2022, "type": "tax"}, "is before"),
        ({"from": "2022", "type": "tax"}, "from must be a whole year"),
        ({"from": True, "type": "tax"}, "from must be a whole year"),
        ({"from": 1800, "type": "tax"}, "from must be a whole year"),
        ({"from": 2022, "to": None, "type": "tax"}, "to must be a whole year"),
        ({"to": 2022, "type": "tax"}, "from must be a whole year"),
        ({"from": 2022, "type": "school"}, "type must be one of"),
        ({"from": 2022}, "type must be one of"),
        ({"from": 2022, "type": "tax", "until": 2023}, "unknown key `until`"),
        ({"from": 2022, "type": "tax", "note": " "}, "note must be a non-blank string"),
        ("2022-2025", "years must be a mapping"),
    ],
)
def test_years(tmp_path, years, fragment):
    issues = check(make_repo(tmp_path), entry(years=years))
    if fragment is None:
        assert issues == []
    else:
        assert any(fragment in issue for issue in issues), issues


RESOLUTION = {"by": "legislation", "effective": "2026-01-01", "record": "https://github.com/x/y/issues/3"}


def test_conflict_and_resolution_go_together(tmp_path):
    ctx = make_repo(tmp_path)
    resolved = entry(conflict="resolved", resolution=dict(RESOLUTION), years={"from": 2022, "to": 2025, "type": "tax"})
    assert check(ctx, resolved) == []
    assert check(ctx, entry(conflict="live")) == []
    cases = (
        (entry(conflict="resolved", years={"from": 2022, "to": 2025, "type": "tax"}), "resolution is required when conflict is `resolved`"),
        (entry(resolution=dict(RESOLUTION)), "resolution must be absent while conflict is `live`"),
        (entry(conflict="live", resolution=dict(RESOLUTION)), "resolution must be absent while conflict is `live`"),
        (entry(conflict="resolved", resolution=dict(RESOLUTION), years={"from": 2022, "type": "tax"}), "years.to is required"),
        (entry(conflict="resolved", resolution=dict(RESOLUTION)), "years.to is required"),
        (entry(conflict="closed"), "conflict must be live or resolved"),
    )
    for value, fragment in cases:
        assert any(fragment in issue for issue in check(ctx, value)), fragment
    for field, value, fragment in (
        ("by", "court", "by must be legislation or agency"),
        ("effective", "2026-02-30", "effective must be a real"),
        ("record", "http://example.com", "record must be an https URL"),
    ):
        bad = copy.deepcopy(resolved)
        bad["resolution"][field] = value
        assert any(fragment in issue for issue in check(ctx, bad)), field


UNPUBLISHED = {"kind": "agency-practice", "description": "MN DHS correspondence on approved guardian fees", "date": "2026-10-19"}


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({}, None),
        ({"description": 'The department "approves one hundred dollars a month" in practice'}, "quotes more than 4 words"),
        ({"description": "The department \u201capproves one hundred dollars a month\u201d"}, "quotes more than 4 words"),
        ({"description": 'The department "approves $100" in practice'}, None),
        ({"description": "Reply from someone@example.gov"}, "contains `@`"),
        ({"kind": "phone"}, "kind must be one of"),
        ({"date": "October 19"}, "date must be a real"),
        ({"description": " "}, "description must be a non-blank string"),
        ({"excerpt": "verbatim words"}, "unknown key `excerpt`"),
    ],
)
def test_unpublished_sources(tmp_path, change, fragment):
    issues = check(make_repo(tmp_path), entry(unpublished_sources=[{**UNPUBLISHED, **change}]))
    if fragment is None:
        assert issues == []
    else:
        assert any(fragment in issue for issue in issues), issues


def test_unpublished_sources_shape(tmp_path):
    ctx = make_repo(tmp_path)
    assert any("unpublished_sources must be a non-empty list" in issue for issue in check(ctx, entry(unpublished_sources=[])))
    missing = {key: value for key, value in UNPUBLISHED.items() if key != "date"}
    assert any("unpublished_sources[0].date is required" in issue for issue in check(ctx, entry(unpublished_sources=[missing])))


# ---------------------------------------------------------------------------
# Adopting modules and the reverse scan


def adopted_entry():
    value = entry()
    del value["pending_texts"]
    value["adopted_by"] = [{"module": "us/statutes/42/403/a/5.yaml", "interpretation": "family-maximum-saving-clause"}]
    return value


def test_adopting_module_carries_the_entry_unchanged(tmp_path):
    ctx = make_repo(tmp_path)
    value = adopted_entry()
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["rule_a"])])
    assert check(ctx, value) == []


def test_adopting_module_drift_is_reported_per_field(tmp_path):
    ctx = make_repo(tmp_path)
    value = adopted_entry()
    carried = sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["rule_a"])
    carried["reading"] = "Something else."
    carried["texts"] = []
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [carried])
    issues = check(ctx, value)
    assert any("`reading` differs" in issue for issue in issues)
    assert any("`texts` differs" in issue for issue in issues)


def test_adopting_module_failures(tmp_path):
    ctx = make_repo(tmp_path)
    value = adopted_entry()
    assert any("does not exist" in issue for issue in check(ctx, value))
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [])
    assert any("has no module.interpretations entry" in issue for issue in check(ctx, value))
    carried = sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["no_such_rule"])
    carried["jurisdiction"] = "us"
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [carried])
    issues = check(ctx, value)
    assert any("affects `no_such_rule`, which is not a rule" in issue for issue in issues)
    assert any("unknown key `jurisdiction`" in issue for issue in issues)


def test_adopted_by_paths_and_pending_texts(tmp_path):
    ctx = make_repo(tmp_path)
    bad = entry(adopted_by=[{"module": "tools/x.yaml", "interpretation": "x"}])
    issues = check(ctx, bad)
    assert any("must be a RuleSpec module path in this repository" in issue for issue in issues)
    assert any("still has pending_texts" in issue for issue in issues)
    test_file = adopted_entry()
    test_file["adopted_by"][0]["module"] = "us/statutes/42/403/a/5.test.yaml"
    assert any("must be a RuleSpec module path" in issue for issue in check(ctx, test_file))


def tension(status="open", interpretation_id="two-provisions"):
    value = {
        "id": interpretation_id,
        "question": "q",
        "texts": [
            {"corpus_citation_path": "us/statute/42/403/a/5", "excerpt": "x"},
            {"corpus_citation_path": "us/regulation/20/404/403", "excerpt": "y"},
        ],
        "reading": "r",
        "alternatives": [{"reading": "s"}],
        "basis": "b",
        "status": status,
        "affects": ["rule_a"],
    }
    if status != "open":
        value["ruling"] = {"by": "Axiom Foundation", "date": "2026-10-12", "record": "https://github.com/x/y/pull/1"}
    return value


def test_unregistered_open_tension_only_warns(tmp_path):
    ctx = make_repo(tmp_path)
    single = {**tension(), "id": "one-provision", "texts": tension()["texts"][:1]}
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [tension(), single])
    assert check(ctx) == []
    failures, warnings = sc.unregistered_tensions(ctx.root, {"version": 1, "entries": []})
    assert failures == []
    assert len(warnings) == 1 and "two-provisions (open) records provisions in tension" in warnings[0]


@pytest.mark.parametrize("status", ["ruled", "confirmed", "contradicted"])
def test_unregistered_decided_tension_fails(tmp_path, status):
    ctx = make_repo(tmp_path)
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [tension(status)])
    issues = check(ctx)
    assert len(issues) == 1 and f"two-provisions ({status}) records provisions in tension" in issues[0]
    assert check(ctx, scan=False) == []


def test_check_cli_writes_warnings(tmp_path, capsys):
    ctx = make_repo(tmp_path)
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [tension()])
    (ctx.root / sc.REGISTER_FILENAME).write_text("version: 1\nentries: []\n")
    out = tmp_path / "warnings.txt"
    assert sc.main(["check", "--root", str(ctx.root), "--warnings-file", str(out)]) == 0
    assert out.read_text().count("\n") == 1 and "two-provisions (open)" in out.read_text()
    assert "1 unregistered open interpretation(s)" in capsys.readouterr().out


def test_registered_tension_is_not_reported(tmp_path):
    ctx = make_repo(tmp_path)
    value = adopted_entry()
    value["texts"].append({"corpus_citation_path": "us/regulation/20/404/403", "excerpt": "y"})
    write_module(ctx, "us/statutes/42/403/a/5.yaml", [sc.to_module_interpretation(value, interpretation_id=value["id"], affects=["rule_a"])])
    assert check(ctx, value) == []


# ---------------------------------------------------------------------------
# Hygiene


@pytest.mark.parametrize(
    "text",
    [
        "Ruled in d1338.",
        "See local_6085d52a-863b for context.",
        "Copied from /Users/someone/notes.md",
        "Copied from ~/notes.md",
        "Ask someone@example.org",
    ],
)
def test_hygiene_flags_private_material(tmp_path, text):
    assert any("the register is public" in issue for issue in check(make_repo(tmp_path), entry(basis=text)))


def test_hygiene_ignores_hashes_and_urls(tmp_path):
    value = entry()
    value["pending_texts"][0]["snapshot"]["commit"] = "d1338" + "0" * 35
    value["pending_texts"][0]["snapshot"]["sha256"] = "d1418" + "0" * 59
    value["ruling"]["record"] = "https://github.com/PolicyEngine/microcosm-dynamics/pull/530#issuecomment-1"
    assert check(make_repo(tmp_path), value) == []


# ---------------------------------------------------------------------------
# Helpers


def test_python_test_exists():
    source = (
        "import pytest\n"
        "def test_a():\n    pass\n"
        "async def test_b():\n    pass\n"
        "class TestC:\n    def test_d(self):\n        pass\n"
        "@pytest.mark.parametrize('x', [pytest.param(1, id='one')])\n"
        "def test_e(x):\n    pass\n"
    )
    assert sc.python_test_exists(source, "test_a")
    assert sc.python_test_exists(source, "test_b")
    assert sc.python_test_exists(source, "TestC::test_d")
    assert sc.python_test_exists(source, "test_e[one]")
    assert not sc.python_test_exists(source, "test_d")  # method, not module level
    assert not sc.python_test_exists(source, "TestC::test_a")
    assert not sc.python_test_exists(source, "test_e[two]")
    assert not sc.python_test_exists("def broken(:\n", "broken")


def test_yaml_test_case_names():
    assert sc.yaml_test_case_names(yaml.safe_dump([{"name": "a"}, {"name": "b"}, "junk"])) == {"a", "b"}
    assert sc.yaml_test_case_names(yaml.safe_dump({"cases": [{"name": "c"}]})) == {"c"}
    assert sc.yaml_test_case_names(": : :") == set()


def test_snapshot_text_strips_markup():
    data = b"<html><head><style>p{}</style><script>var x='<p>hidden</p>';</script></head><body><p>A&nbsp;rule &amp; its\n\n  <b>exception</b>&#8217;s text</p></body></html>"
    assert sc.snapshot_text(data, "x.html") == "A rule & its exception’s text"
    assert sc.snapshot_text(b"<P>Block one</P><P>block <E T=\"03\">two</E><SU>1</SU></P>", "x.xml") == "Block one block two1"
    assert sc.snapshot_text(b"plain\n text ", "x.txt") == "plain text"
    with pytest.raises(ValueError):
        sc.snapshot_text(b"%PDF", "x.pdf")


def test_repo_jurisdiction_ok():
    assert sc.repo_jurisdiction_ok("TheAxiomFoundation/rulespec-us", "us")
    assert sc.repo_jurisdiction_ok("TheAxiomFoundation/rulespec-us", "us-co")
    assert sc.repo_jurisdiction_ok("TheAxiomFoundation/rulespec-uk", "uk-babergh")
    assert not sc.repo_jurisdiction_ok("TheAxiomFoundation/rulespec-us", "uk")
    assert not sc.repo_jurisdiction_ok("TheAxiomFoundation/rulespec-us", "usx")
    assert not sc.repo_jurisdiction_ok("TheAxiomFoundation/axiom-corpus", "us")


# ---------------------------------------------------------------------------
# verify, with a fake network


class FakeNet:
    def __init__(self, files):
        self.files = files

    def __call__(self, repo, commit, path):
        try:
            return self.files[(repo, commit, path)]
        except KeyError:
            raise OSError(f"404 {repo}@{commit}:{path}") from None


def substring(source_text, excerpt):
    return excerpt in source_text


def verified_inputs():
    value = entry()
    snapshot = b"<p>Total family payment is less after the PIA increase than it was before.</p>"
    value["pending_texts"][0]["snapshot"]["sha256"] = hashlib.sha256(snapshot).hexdigest()
    files = {
        ("PolicyEngine/microcosm-dynamics", COMMIT, "tests/data/poms_rs_00630_400.html"): snapshot,
        ("PolicyEngine/microcosm-dynamics", COMMIT, "src/gross_benefits.py"): b"def g():\n    pass\n",
        ("PolicyEngine/microcosm-dynamics", COMMIT, "tests/test_g.py"): (
            b"def test__given_operational__then_pays():\n    pass\n"
            b"class TestLiteral:\n    def test_refuses(self):\n        pass\n# ids: child-ages-out\n"
        ),
    }
    corpus = {"us/statute/42/403/a/5": "... the benefit for any such subsequent month shall be ..."}
    return {"version": 1, "entries": [value]}, FakeNet(files), corpus


def test_verify_passes_and_reports_lag_and_uncited():
    payload, net, corpus = verified_inputs()
    issues, report = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert issues == []
    assert report[0] == "verified: 1 texts, 1 pending texts, 1 implementations, 2 tests"
    assert any(line.startswith("uncited:") for line in report)
    assert "lags: us:family-maximum-saving-clause (entries[0]).implementations[0] follows every-later-month, not the reading it adopts (reading)" in report


def test_verify_lag_is_measured_against_what_the_consumer_adopts():
    payload, net, corpus = verified_inputs()
    implementation = payload["entries"][0]["implementations"][0]
    implementation["adopts"] = "every-later-month"
    implementation["basis"] = "Follows the statute."
    issues, report = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert issues == []
    assert not any(line.startswith("lags:") for line in report)
    implementation["follows"] = "reading"
    _, report = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert any("follows reading, not the reading it adopts (every-later-month)" in line for line in report)


def test_verify_lists_unpublished_sources_as_unverifiable():
    payload, net, corpus = verified_inputs()
    payload["entries"][0]["unpublished_sources"] = [dict(UNPUBLISHED)]
    issues, report = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert issues == []
    assert any(line.startswith("unverifiable:") and "MN DHS correspondence" in line for line in report)


def test_verify_findings():
    payload, net, corpus = verified_inputs()
    value = payload["entries"][0]
    value["texts"][0]["excerpt"] = "for every subsequent month"
    value["pending_texts"][0]["excerpt"] = "Total family payment is more"
    value["implementations"][0]["cites_entry"] = True
    value["tests"][0]["name"] = "test_missing"
    issues, _ = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert any("not verbatim at us/statute/42/403/a/5" in issue for issue in issues)
    assert any("not verbatim in tests/data/poms_rs_00630_400.html" in issue for issue in issues)
    assert any("cites_entry is true but the file does not contain" in issue for issue in issues)
    assert any("test_missing is not defined" in issue for issue in issues)


def test_verify_sha_mismatch_unresolved_and_no_corpus():
    payload, net, _ = verified_inputs()
    payload["entries"][0]["pending_texts"][0]["snapshot"]["sha256"] = SHA
    issues, _ = sc.verify_register(payload, fetch=net, matches=substring, resolve={}.get)
    assert any("snapshot sha256 is" in issue for issue in issues)
    assert any("is not in the pinned corpus release" in issue for issue in issues)
    issues, _ = sc.verify_register(payload, fetch=net, matches=substring, resolve=None)
    assert any("not verified; no pinned corpus release" in issue for issue in issues)


def test_verify_cites_entry_true_when_file_cites():
    payload, net, corpus = verified_inputs()
    key = ("PolicyEngine/microcosm-dynamics", COMMIT, "src/gross_benefits.py")
    net.files[key] = b"# Source conflict us:family-maximum-saving-clause\n"
    issues, _ = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert any("cites_entry is false but the file contains" in issue for issue in issues)
    payload["entries"][0]["implementations"][0]["cites_entry"] = True
    assert sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)[0] == []


def test_verify_unreadable_files():
    payload, _, corpus = verified_inputs()
    issues, _ = sc.verify_register(payload, fetch=FakeNet({}), matches=substring, resolve=corpus.get)
    assert sum("cannot read" in issue for issue in issues) == 4


# ---------------------------------------------------------------------------
# aggregate


def test_aggregate_rows_and_collisions():
    us = {"version": 1, "entries": [entry(), entry(id="other", jurisdiction="us-co")]}
    uk = {"version": 1, "entries": [entry(jurisdiction="uk-babergh"), entry(id="misplaced", jurisdiction="us")]}
    rows, issues = sc.aggregate({"TheAxiomFoundation/rulespec-uk": uk, "TheAxiomFoundation/rulespec-us": us})
    ids = [row["global_id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert "uk-babergh:family-maximum-saving-clause" in ids and "us:family-maximum-saving-clause" in ids
    assert issues == ["TheAxiomFoundation/rulespec-uk: us:misplaced has a jurisdiction outside that repository."]
    rows, issues = sc.aggregate({"TheAxiomFoundation/rulespec-us": us, "TheAxiomFoundation/rulespec-us-dup": us})
    assert any("is in both" in issue for issue in issues)
    markdown = sc.render_markdown(rows)
    assert markdown.startswith("# Source conflicts across Axiom registers")
    assert "21.00 USD/month" in markdown


def test_aggregate_carries_years_kinds_conflict_and_adopts():
    value = entry(years={"from": 2022, "to": 2025, "type": "tax"}, unpublished_sources=[dict(UNPUBLISHED)])
    value["implementations"][0]["adopts"] = "every-later-month"
    rows, issues = sc.aggregate({"TheAxiomFoundation/rulespec-us": {"version": 1, "entries": [value]}})
    assert issues == []
    row = rows[0]
    assert row["years"] == {"from": 2022, "to": 2025, "type": "tax"}
    assert row["conflict"] == "live"
    assert row["source_kinds"] == ["agency-practice", "manual", "statute"]
    assert row["alternatives"] == [{"id": "every-later-month", "reading": "Protect every later month."}]
    assert row["implementations"][0]["adopts"] == "every-later-month"
    markdown = sc.render_markdown(rows)
    assert "2022\u20132025 (tax)" in markdown
    assert "agency-practice, manual, statute" in markdown


def test_citation_label():
    assert sc.citation_label("us/statute/42/403/f/1") == "42 U.S.C. 403(f)(1)"
    assert sc.citation_label("us/statute/26/25A") == "26 U.S.C. 25A"
    assert sc.citation_label("us/regulation/20/404/435") == "20 CFR 404.435"
    assert sc.citation_label("us/regulation/7/273/24/a") == "7 CFR 273.24(a)"
    assert sc.citation_label("uk-babergh/manual/scheme/page-87") == "uk-babergh/manual/scheme/page-87"
    assert sc.citation_label("us/statute/hts/chapter-99") == "us/statute/hts/chapter-99"


def test_aggregate_sources_are_unique_citations():
    value = entry()
    value["pending_texts"].append(dict(value["pending_texts"][0]))
    rows, _ = sc.aggregate({"TheAxiomFoundation/rulespec-us": {"entries": [value]}})
    assert rows[0]["sources"] == ["42 U.S.C. 403(a)(5)", "POMS RS 00630.400"]


def test_aggregate_cli_with_local_registers(tmp_path, capsys):
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "entries": [entry()]}))
    out = tmp_path / "rows.json"
    assert sc.main(["aggregate", "--register", f"TheAxiomFoundation/rulespec-us={path}", "--json", str(out)]) == 0
    assert '"global_id": "us:family-maximum-saving-clause"' in out.read_text()


# ---------------------------------------------------------------------------
# Proposed implementations and shipped commits

PROPOSED = {
    "repo": "PolicyEngine/policyengine-us",
    "pr": "https://github.com/PolicyEngine/policyengine-us/pull/10200",
    "path": "policyengine_us/variables/gov/states/hi/tax/income/claim_of_right/hi_claim_of_right_deduction.py",
    "follows": "reading",
}


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({}, None),
        ({"follows": "every-later-month"}, None),
        ({"pr": "https://github.com/PolicyEngine/policyengine-us/issues/10200"}, "pr must be a github.com pull request URL"),
        ({"pr": "http://github.com/PolicyEngine/policyengine-us/pull/1"}, "pr must be a github.com pull request URL"),
        ({"pr": "https://github.com/PolicyEngine/other/pull/1"}, "is a pull request in PolicyEngine/other"),
        ({"follows": "statute"}, "follows names `statute`"),
        ({"path": "/abs/x.py"}, "path must be a relative path"),
        ({"repo": "nope"}, "repo must be owner/name"),
        ({"commit": COMMIT}, "unknown key `commit`"),
    ],
)
def test_proposed_implementations(tmp_path, change, fragment):
    issues = check(make_repo(tmp_path), entry(proposed=[{**PROPOSED, **change}]))
    if fragment is None:
        assert issues == []
    else:
        assert any(fragment in issue for issue in issues), issues


def test_proposed_must_be_a_non_empty_list(tmp_path):
    assert any("proposed must be a non-empty list" in issue for issue in check(make_repo(tmp_path), entry(proposed=[])))


def test_verify_requires_shipped_commits():
    payload, net, corpus = verified_inputs()
    seen = []

    def on_default_branch(repo, commit):
        seen.append((repo, commit))
        return False

    issues, _ = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get, on_default_branch=on_default_branch)
    labels = [issue.split(":")[1] for issue in issues if "is not on" in issue]
    # The snapshot, the implementation and both tests are external commits.
    assert len([issue for issue in issues if "is not on PolicyEngine/microcosm-dynamics's default branch" in issue]) == 4, issues
    assert {repo for repo, _ in seen} == {"PolicyEngine/microcosm-dynamics"}
    assert labels
    shipped, _ = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get, on_default_branch=lambda repo, commit: True)
    assert shipped == []


def test_verify_reports_when_reachability_is_unchecked_or_unknown():
    payload, net, corpus = verified_inputs()
    issues, report = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert issues == []
    assert any(line.startswith("unchecked: default-branch reachability") for line in report)

    def broken(repo, commit):
        raise OSError("network down")

    issues, _ = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get, on_default_branch=broken)
    assert any("cannot tell whether" in issue for issue in issues)


def test_verify_lists_proposed_implementations():
    payload, net, corpus = verified_inputs()
    payload["entries"][0]["proposed"] = [dict(PROPOSED)]
    issues, report = sc.verify_register(payload, fetch=net, matches=substring, resolve=corpus.get)
    assert issues == []
    assert any(line.startswith("proposed:") and "pull/10200" in line for line in report)
