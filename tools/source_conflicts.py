"""Validate, verify and aggregate the source-conflict register.

`known-source-conflicts.yaml`, at the root of a rulespec repository, records
the places where a jurisdiction's sources disagree (statute against
regulation, regulation against an agency manual, one manual section against
another), the readings each side supports, and the reading adopted. Every
rulespec repository keeps its own register; the Axiom-wide view is generated
by aggregating them (`aggregate`), never kept as a second copy.

Each entry's interpretation core has the field names and status rules of a
RuleSpec `module.interpretations` v1 entry (question, texts, reading,
alternatives, basis, status, ruling, confirmation), so an entry moves into a
module unchanged when its provision is encoded; the module then adds
`affects`. Register-only fields carry what a module cannot: the jurisdiction
and program, provisions not yet in the corpus (`pending_texts`, checked
against pinned snapshots), whether the adopted reading computes or refuses
contested cases (`outcome`), the modules that adopt the entry as
(module path, interpretation id) pairs (`adopted_by`), code outside RuleSpec
that adopts a reading (`implementations`), the tests that pin each reading
(`tests`) and an invented money case (`money_case`).

Three commands:

- `check` (offline; runs inside the required Repository Checks pytest leg via
  tests/test_source_conflicts.py): the schema, the status rules, public-repo
  hygiene, and every reference into this checkout: adopting modules carry the
  entry verbatim as a `module.interpretations` entry whose `affects` names
  real rules, in-repo tests exist, and every ruled, confirmed or
  contradicted module interpretation whose texts cite two or more provisions
  (provisions in tension) is registered; an unregistered open one is a
  warning, listed by the workflow.
- `verify` (network; the source-conflicts workflow): every `texts` excerpt is
  verbatim at its citation in the pinned corpus release, and every
  `pending_texts` excerpt is verbatim in its pinned snapshot, both judged by
  axiom-encode's own proof-atom matcher; snapshot hashes match; external
  implementations and tests exist at their pinned commits, and each
  implementation's `cites_entry` claim matches the file.
- `aggregate` (network): collect every rulespec repository's register into
  one JSON or Markdown view, checking that global ids are unique and each
  entry's jurisdiction belongs to its repository.

See tools/README.md ("Source-conflict register").
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REGISTER_FILENAME = "known-source-conflicts.yaml"
SCHEMA_VERSION = 1
CONTENT_DIRS = ("statutes", "regulations", "policies", "legislation")
ORGANISATION = "TheAxiomFoundation"

KEBAB_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
JURISDICTION_RE = re.compile(r"[a-z]{2}(-[a-z0-9]+)*")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
DECIMAL_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
CURRENCY_RE = re.compile(r"[A-Z]{3}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A canonical corpus citation path: jurisdiction, document class, then one or
# more segments (us/statute/42/403/a/5, uk-babergh/manual/<doc>/page-87).
# `verify` additionally runs axiom-encode's own canonical-path check.
CORPUS_PATH_RE = re.compile(
    r"[a-z]{2}(-[a-z0-9]+)*/[a-z][a-z-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+"
)
TEST_NAME_RE = re.compile(r"(?P<scope>[A-Za-z_][A-Za-z0-9_]*::)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
INLINE_TAG_RE = re.compile(
    r"(?i)</?(?:a|abbr|b|bdi|bdo|cite|code|data|dfn|e|em|font|fr|i|kbd|mark|q|s|samp"
    r"|small|span|strong|su|sub|sup|time|u|var|wbr)\b[^>]*>"
)
PERIODS = ("once", "week", "month", "year")
STATUSES = ("open", "ruled", "confirmed", "contradicted")
OUTCOMES = ("computed", "refused")
# A reading reference (`follows`, `adopts`, `pins`) is `reading` (the entry's
# adopted reading) or the `id` of one of its alternatives. Positions are never
# used: inserting or reordering an alternative must not flip a consumer's pick.
ADOPTED_READING = "reading"

# What kind of source a text is. Corpus `texts` take their kind from the
# document class in their citation path (DOCUMENT_CLASS_KINDS); `text_kinds`
# may narrow it to another kind that class allows. `pending_texts` and
# `unpublished_sources` state theirs.
SOURCE_KINDS = (
    "statute",
    "regulation",
    "rulemaking",
    "form",
    "instructions",
    "worksheet",
    "manual",
    "guidance",
    "policy",
    "plan",
    "calculator",
    "agency-practice",
)
# Corpus document class (the second segment of a citation path) -> the kinds a
# text in that class may be; the first is the kind derived when `text_kinds`
# names none. A class without a row fails closed: a text citing it is a
# finding until the class gets a row here, so a new class never silently
# allows every kind. tests/test_source_conflicts.py checks every class the
# corpus uses has a row.
DOCUMENT_CLASS_KINDS: dict[str, tuple[str, ...]] = {
    "statute": ("statute",),
    "regulation": ("regulation",),
    "rulemaking": ("rulemaking",),
    "form": ("form", "instructions", "worksheet"),
    "guidance": ("guidance", "instructions", "calculator", "plan"),
    "manual": ("manual",),
    "policy": ("policy", "manual", "guidance", "plan"),
    "district-plan": ("plan",),
}
# `years.type`: which year the bounds count. `program` needs `years.note`
# saying what the program year is (SNAP's federal fiscal year, a LIHEAP
# program year from October, ...).
YEAR_TYPES = ("calendar", "tax", "federal-fiscal", "state-fiscal", "program")
MIN_YEAR, MAX_YEAR = 1900, 2100
# `conflict`: whether the sources still disagree. `resolved` needs a
# `resolution` (who closed it, when, and a public record) and `years.to`;
# `live` (the default) forbids a `resolution`.
CONFLICT_STATES = ("live", "resolved")
RESOLVERS = ("legislation", "agency")
# An unpublished source (correspondence, a call) is described, never quoted:
# a description may hold at most this many words inside quotation marks.
MAX_QUOTED_WORDS = 4
QUOTED_RE = re.compile(r'["\u201c]([^"\u201c\u201d]+)["\u201d]')

# The interpretation core: compared field by field with a module's
# `module.interpretations` entry, and the only fields a module may carry
# besides `id` and `affects`.
INTERPRETATION_CORE = (
    "question",
    "texts",
    "reading",
    "alternatives",
    "basis",
    "status",
    "ruling",
    "confirmation",
)
MODULE_INTERPRETATION_KEYS = frozenset({"id", *INTERPRETATION_CORE, "affects"})
ENTRY_KEYS = frozenset(
    {
        "id",
        "jurisdiction",
        "program",
        *INTERPRETATION_CORE,
        "pending_texts",
        "outcome",
        "adopted_by",
        "implementations",
        "tests",
        "money_case",
        "years",
        "conflict",
        "resolution",
        "text_kinds",
        "unpublished_sources",
        "proposed",
    }
)
# A pull request URL on github.com: proposed implementations name the PR that
# would add the reading, before it merges.
PR_URL_RE = re.compile(r"https://github\.com/(?P<repo>[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)/pull/[1-9][0-9]*")
REQUIRED_ENTRY_KEYS = (
    "id",
    "jurisdiction",
    "program",
    "question",
    "texts",
    "reading",
    "alternatives",
    "basis",
    "status",
    "outcome",
)

# Public-repo hygiene: the register is published, so it never carries
# decision-queue ids, session ids, local paths or email addresses.
HYGIENE_PATTERNS = (
    (re.compile(r"\bd[0-9]{3,5}\b"), "a decision-queue id"),
    (re.compile(r"\blocal_[0-9a-f]{8}"), "a session id"),
    (re.compile(r"(?:/Users/|/home/|/private/tmp|(?<![\w.])~/)"), "a local path"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z][\w.-]*"), "an email address"),
)


class RegisterError(ValueError):
    """The register file cannot be read as YAML."""


# ---------------------------------------------------------------------------
# Loading


def load_register(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        raise RegisterError(f"{path} must be a regular file")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RegisterError(f"{path} is not valid UTF-8 YAML: {exc}") from exc


def global_id(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('jurisdiction')}:{entry.get('id')}"


def alternative_ids(alternatives: Any) -> list[str]:
    """The ids of an entry's alternatives, in order (non-strings skipped)."""

    if not isinstance(alternatives, list):
        return []
    return [
        alternative["id"]
        for alternative in alternatives
        if isinstance(alternative, dict) and isinstance(alternative.get("id"), str)
    ]


def document_class(corpus_citation_path: str) -> str:
    """us/statute/42/403/a/5 -> statute."""

    return corpus_citation_path.split("/")[1]


def text_kind(entry: Mapping[str, Any], corpus_citation_path: str) -> str | None:
    """The kind of a corpus text: its `text_kinds` override, else its class's
    default; None for a class without a DOCUMENT_CLASS_KINDS row."""

    overrides = entry.get("text_kinds")
    if isinstance(overrides, dict) and isinstance(overrides.get(corpus_citation_path), str):
        return overrides[corpus_citation_path]
    allowed = DOCUMENT_CLASS_KINDS.get(document_class(corpus_citation_path))
    return allowed[0] if allowed else None


# ---------------------------------------------------------------------------
# Field helpers (the interpretation rules mirror axiom-encode's
# _validate_module_interpretations so an entry that passes here passes there)


def _mapping(value: Any, *, label: str, allowed: Iterable[str], issues: list[str]) -> bool:
    if not isinstance(value, dict):
        issues.append(f"{label} must be a mapping.")
        return False
    allowed_set = set(allowed)
    for key in value:
        if key not in allowed_set:
            issues.append(f"{label} has unknown key `{key}`.")
    return True


def _string(value: Any, *, label: str, issues: list[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label} must be a non-blank string.")
        return False
    return True


def _nonempty_list(value: Any, *, label: str, issues: list[str]) -> bool:
    if not isinstance(value, list) or not value:
        issues.append(f"{label} must be a non-empty list.")
        return False
    return True


def _https_url(value: Any) -> bool:
    if not isinstance(value, str) or re.search(r"\s", value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname)


def _real_date(value: Any) -> bool:
    if type(value) is dt.date:  # an unquoted YAML date
        return True
    if isinstance(value, str) and DATE_RE.fullmatch(value):
        with contextlib.suppress(ValueError):
            dt.date.fromisoformat(value)
            return True
    return False


def _relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _check_record(record: Any, *, label: str, confirmation: bool, issues: list[str]) -> None:
    allowed = {"by", "date", "record"} | ({"excerpt"} if confirmation else set())
    if not _mapping(record, label=label, allowed=allowed, issues=issues):
        return
    _string(record.get("by"), label=f"{label}.by", issues=issues)
    if confirmation:
        _string(record.get("excerpt"), label=f"{label}.excerpt", issues=issues)
    if not _real_date(record.get("date")):
        issues.append(f"{label}.date must be a real YYYY-MM-DD date.")
    if (not confirmation or "record" in record) and not _https_url(record.get("record")):
        issues.append(f"{label}.record must be an https URL.")


def check_interpretation_core(entry: Mapping[str, Any], *, label: str, require_texts: bool) -> list[str]:
    """The module.interpretations v1 rules, minus `id` and `affects`."""

    issues: list[str] = []
    for name in ("question", "reading", "basis"):
        _string(entry.get(name), label=f"{label}.{name}", issues=issues)
    texts = entry.get("texts")
    if not isinstance(texts, list) or (require_texts and not texts):
        issues.append(
            f"{label}.texts must be a non-empty list."
            if require_texts
            else f"{label}.texts must be a list."
        )
    else:
        for index, text in enumerate(texts):
            text_label = f"{label}.texts[{index}]"
            if not _mapping(text, label=text_label, allowed={"corpus_citation_path", "excerpt"}, issues=issues):
                continue
            path = text.get("corpus_citation_path")
            if _string(path, label=f"{text_label}.corpus_citation_path", issues=issues):
                if CORPUS_PATH_RE.fullmatch(path) is None:
                    issues.append(f"{text_label}.corpus_citation_path is not a canonical corpus citation path.")
            _string(text.get("excerpt"), label=f"{text_label}.excerpt", issues=issues)
    alternatives = entry.get("alternatives")
    if _nonempty_list(alternatives, label=f"{label}.alternatives", issues=issues):
        seen_ids: set[str] = set()
        for index, alternative in enumerate(alternatives):
            alt_label = f"{label}.alternatives[{index}]"
            if not _mapping(alternative, label=alt_label, allowed={"id", "reading", "effect"}, issues=issues):
                continue
            alternative_id = alternative.get("id")
            if "id" not in alternative:
                issues.append(f"{alt_label}.id is required; references name an alternative by id, never by position.")
            elif not (isinstance(alternative_id, str) and KEBAB_RE.fullmatch(alternative_id)):
                issues.append(f"{alt_label}.id must match [a-z0-9]+(-[a-z0-9]+)*.")
            elif alternative_id == ADOPTED_READING:
                issues.append(f"{alt_label}.id must not be `{ADOPTED_READING}`, which names the adopted reading.")
            elif alternative_id in seen_ids:
                issues.append(f"{alt_label}.id `{alternative_id}` repeats an earlier alternative's id.")
            else:
                seen_ids.add(alternative_id)
            _string(alternative.get("reading"), label=f"{alt_label}.reading", issues=issues)
            if "effect" in alternative:
                _string(alternative.get("effect"), label=f"{alt_label}.effect", issues=issues)
    status = entry.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        issues.append(f"{label}.status must be open, ruled, confirmed or contradicted.")
    else:
        for field, required in (
            ("ruling", status != "open"),
            ("confirmation", status in {"confirmed", "contradicted"}),
        ):
            if not required:
                if field in entry:
                    issues.append(f"{label}.{field} must be absent when status is `{status}`.")
            elif field not in entry:
                issues.append(f"{label}.{field} must be a mapping when status is `{status}`.")
            else:
                _check_record(entry[field], label=f"{label}.{field}", confirmation=field == "confirmation", issues=issues)
        if status == "contradicted":
            issues.append(
                f"{label}: the publisher contradicted this reading; repair the encoding, move the "
                "old reading into alternatives, and set the new status."
            )
    return issues


# ---------------------------------------------------------------------------
# Repository context


@dataclass(frozen=True)
class RepoContext:
    root: Path
    repo: str  # owner/name of this checkout, e.g. TheAxiomFoundation/rulespec-us

    def jurisdictions(self) -> frozenset[str]:
        return frozenset(
            child.name
            for child in self.root.iterdir()
            if child.is_dir()
            and JURISDICTION_RE.fullmatch(child.name)
            and any((child / marker).is_dir() for marker in CONTENT_DIRS)
        )

    def module_path_ok(self, value: str) -> bool:
        parts = PurePosixPath(value).parts
        return (
            len(parts) >= 3
            and parts[0] in self.jurisdictions()
            and parts[1] in CONTENT_DIRS
            and value.endswith(".yaml")
            and not value.endswith(".test.yaml")
        )


def iter_module_files(root: Path) -> Iterator[Path]:
    for jurisdiction in sorted(root.iterdir()):
        if not (jurisdiction.is_dir() and JURISDICTION_RE.fullmatch(jurisdiction.name)):
            continue
        for marker in CONTENT_DIRS:
            base = jurisdiction / marker
            if base.is_dir():
                yield from sorted(
                    path for path in base.rglob("*.yaml") if not path.name.endswith(".test.yaml")
                )


def module_interpretations(path: Path) -> tuple[list[dict[str, Any]], set[str]] | None:
    """(interpretations, rule names) of one module, or None when it has none."""

    text = path.read_text(encoding="utf-8")
    if "interpretations" not in text:
        return None
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    module = payload.get("module") if isinstance(payload, dict) else None
    entries = module.get("interpretations") if isinstance(module, dict) else None
    if not isinstance(entries, list):
        return None
    rules = payload.get("rules")
    names = (
        {rule["name"] for rule in rules if isinstance(rule, dict) and isinstance(rule.get("name"), str)}
        if isinstance(rules, list)
        else set()
    )
    return [entry for entry in entries if isinstance(entry, dict)], names


def to_module_interpretation(entry: Mapping[str, Any], *, interpretation_id: str, affects: list[str]) -> dict[str, Any]:
    """The module.interpretations entry a register entry becomes when encoded."""

    result: dict[str, Any] = {"id": interpretation_id}
    for name in INTERPRETATION_CORE:
        if name in entry:
            result[name] = entry[name]
    result["affects"] = list(affects)
    return result


def interpretation_core(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {name: entry[name] for name in INTERPRETATION_CORE if name in entry}


def yaml_test_case_names(text: str) -> set[str]:
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        return set()
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        return set()
    return {case["name"] for case in cases if isinstance(case, dict) and isinstance(case.get("name"), str)}


# A Python test reference names a whole test function that pytest's default
# collection picks up from the file alone. The rules below are a screen for
# the ways an honest test file stops a test being collected, not a proof
# against deliberately obfuscated import-time code (see tools/README.md, Test
# references). They accept a deliberately narrow shape and refuse everything
# else (fail closed):
# - names every common pytest configuration collects: files test_*.py,
#   functions test_*, classes Test* (python_files, python_functions and
#   python_classes are not read);
# - a plain def (not async, not a generator) that is the final binding of its
#   name at module level, or in a Test* class at module level that has no
#   bases, no metaclass, no __init__/__new__ and no __test__;
# - decorators limited to pytest marks, Hypothesis and unittest.mock.patch,
#   resolved through imports bound nowhere else: anything else (a fixture,
#   property, dataclass or a home-made wrapper) can stop pytest collecting it;
# - nothing visible that rebinds the function, its class or an imported name:
#   a later store, del or attribute set (in a class body too), a call that
#   takes the function other than a pytest mark, `global`, or exec, eval,
#   globals(), vars() or sys.modules in code that runs at import;
# - no __test__ or store to __test__ in any form, no module __getattr__, a
#   pytestmark of marks only, no shadowed `object` behind a class base, no
#   pytest.skip/importorskip/xfail/fail/exit or raise in code that runs at
#   import, and no generator test anywhere (pytest 9 refuses the whole module).
# "Code that runs at import" is module-level statements, a main guard's else
# branch (not its body), class bodies, and annotations (eager before 3.14).
# A parametrized case id ([...]) is refused: pytest builds ids from sources the
# file does not show.
#
# Whether a collected test runs and passes belongs to the owning repository's
# CI. A skip in its body, fixtures or setup functions, a skip or xfail mark, an
# empty parameter set or Hypothesis phases can stop it running without failing
# anything; `python_test_warnings` reports the ones the file shows. Conftest
# fixtures and hooks, plugins, ini or addopts selection and runtime rebinding
# can do the same, and nothing here checks for them.
PYTHON_TEST_FILE_RE = re.compile(r"test_[^/]*\.py")
_TEST_DECORATORS = frozenset(
    {
        "hypothesis.example",
        "hypothesis.given",
        "hypothesis.seed",
        "hypothesis.settings",
        "unittest.mock.patch",
        "unittest.mock.patch.dict",
        "unittest.mock.patch.multiple",
        "unittest.mock.patch.object",
    }
)
_SKIP_MARKS = frozenset({"skip", "skipif", "xfail"})
_SKIP_CALLS = frozenset({"pytest.importorskip", "pytest.skip", "pytest.xfail"})
_MODULE_STOPS = _SKIP_CALLS | {"pytest.exit", "pytest.fail"}
_REBINDING_BUILTINS = frozenset({"eval", "exec", "globals", "vars"})
_SETUPS = {"module": ("setup_module", "setup_function"), "class": ("setup_class", "setup_method")}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def python_test_file(path: str) -> bool:
    return PYTHON_TEST_FILE_RE.fullmatch(PurePosixPath(path).name) is not None


def _scope_walk(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node evaluated in this scope, including nested blocks, but not
    the bodies of nested functions, classes or lambdas (their names, decorators,
    bases and defaults belong to this scope)."""

    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            outer: list[ast.AST] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                outer.extend(node.args.defaults)
                outer.extend(d for d in node.args.kw_defaults if d is not None)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Annotations run in this scope (eagerly before Python 3.14).
                arguments = node.args
                every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
                outer.extend(arg.annotation for arg in every if arg is not None and arg.annotation is not None)
                if node.returns is not None:
                    outer.append(node.returns)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                outer.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                outer.extend(node.bases)
                outer.extend(keyword.value for keyword in node.keywords)
            stack.extend(reversed(outer))
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _root(node: ast.expr) -> str | None:
    """`a.b[c].d` -> "a"."""

    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _binds(node: ast.AST, name: str) -> bool:
    """Does this node bind, rebind, delete or mutate `name` in its scope?"""

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, (ast.Store, ast.Del))
    if isinstance(node, (ast.Attribute, ast.Subscript)):  # name.a = ..., name.__dict__[k] = ..., del name.a
        return isinstance(node.ctx, (ast.Store, ast.Del)) and _root(node) == name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names)
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return name in node.names
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return node.name == name
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    return False


def _passes(node: ast.AST, name: str) -> bool:
    """A call that receives `name` itself, which could mutate or replace it."""

    if not isinstance(node, ast.Call):
        return False
    values = [*node.args, *(keyword.value for keyword in node.keywords)]
    return any(
        _root(value.value if isinstance(value, ast.Starred) else value) == name for value in values
    )


def _final_definition(body: list[ast.stmt], name: str, imports: dict[str, str]) -> ast.stmt | None:
    """The top-level def or class `name` in this body, when nothing after it in
    the scope rebinds, deletes or mutates the name, or hands it to a call other
    than a pytest mark (pytest sees the final object), else None."""

    found = None
    for index, node in enumerate(body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            found = index
    if found is None:
        return None
    later = list(_scope_walk(body[found + 1 :]))
    # Class bodies run at import too; there a plain name binds a class
    # attribute, but a store under the name or a call taking it reaches it.
    in_classes = [
        node
        for statement in body
        for cls in _scope_walk([statement])
        if isinstance(cls, ast.ClassDef)
        for node in _import_time(cls.body)
    ]
    if any(_binds(node, name) for node in later):
        return None
    if any(isinstance(node, (ast.Attribute, ast.Subscript)) and _binds(node, name) for node in in_classes):
        return None
    if any(_passes(node, name) and not _is_mark(node, imports, applied=True) for node in later + in_classes):
        return None
    if any(_binds(node, name) for node in _scope_walk([body[found]]) if node is not body[found]):
        return None  # e.g. the decorator rebinds it with a walrus
    return body[found]


def _imports(tree: ast.Module) -> dict[str, str]:
    """Local name -> imported dotted name, for names bound only by imports:
    not rebound at module level, not declared `global` anywhere, and with no
    store anywhere under them (`pytest.mark.slow = ...`)."""

    seen: dict[str, set[str]] = {}
    for node in _scope_walk(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    seen.setdefault(alias.asname, set()).add(alias.name)
                else:
                    root = alias.name.split(".")[0]
                    seen.setdefault(root, set()).add(root)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                seen.setdefault(alias.asname or alias.name, set()).add(f"{node.module}.{alias.name}")
    everywhere = list(ast.walk(tree))
    names: dict[str, str] = {}
    for local, targets in seen.items():
        rebound = any(
            _binds(node, local) and not isinstance(node, (ast.Import, ast.ImportFrom))
            for node in _scope_walk(tree.body)
        ) or any(
            isinstance(node, (ast.Global, ast.Nonlocal)) and local in node.names
            or isinstance(node, (ast.Attribute, ast.Subscript)) and _binds(node, local)
            for node in everywhere
        )
        if len(targets) == 1 and not rebound:
            names[local] = next(iter(targets))
    return names


def _every_import(tree: ast.Module) -> dict[str, set[str]]:
    """Local name -> every dotted name any import anywhere binds it to,
    trusted or not. Refusals and warnings resolve through this, so a name
    made untrusted (`pytest.foo = 1`) cannot hide a `pytest.skip`."""

    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                found.setdefault(local, set()).add(alias.name if alias.asname else local)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                found.setdefault(alias.asname or alias.name, set()).add(f"{node.module}.{alias.name}")
    return found


def _possible(node: ast.expr, every: dict[str, set[str]]) -> set[str]:
    """Every dotted name `node` (or the function a call names) could be."""

    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if not isinstance(target, ast.Name):
        return set()
    return {".".join([base, *reversed(parts)]) for base in every.get(target.id, ())}


def _qualified(node: ast.expr, imports: dict[str, str]) -> str | None:
    """`pt.mark.slow(...)` with `import pytest as pt` -> "pytest.mark.slow"."""

    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if not isinstance(target, ast.Name) or target.id not in imports:
        return None
    return ".".join([imports[target.id], *reversed(parts)])


def _mark_name(node: ast.expr, imports: dict[str, str]) -> str | None:
    """"skip" for `pytest.mark.skip` or `pytest.mark.skip(...)`, else None."""

    name = _qualified(node, imports)
    if name is None or not name.startswith("pytest.mark."):
        return None
    mark = name.removeprefix("pytest.mark.")
    return None if "." in mark else mark


def _is_mark(node: ast.expr, imports: dict[str, str], *, applied: bool = False) -> bool:
    """A pytest mark: as a decorator or in pytestmark, or (applied) called on
    the test, `pytest.mark.slow(test_x)`. A bare `parametrize` is an error."""

    mark = _mark_name(node.func if applied and isinstance(node, ast.Call) else node, imports)
    if mark is None:
        return False
    return mark != "parametrize" or applied or isinstance(node, ast.Call)


def _collects_with(node: ast.expr, imports: dict[str, str]) -> bool:
    return _is_mark(node, imports) or _qualified(node, imports) in _TEST_DECORATORS


def _marks(value: ast.expr | None, imports: dict[str, str]) -> list[ast.expr] | None:
    """The marks a pytestmark value holds, or None if it is not only marks."""

    items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
    return items if all(item is not None and _is_mark(item, imports) for item in items) else None


def _pytestmarks(body: list[ast.stmt], imports: dict[str, str]) -> list[ast.expr] | None:
    """Every mark a scope's pytestmark assignments hold, or None when one of
    them is anything but a plain assignment of marks."""

    found: list[ast.expr] = []
    for node in body:
        if not any(_binds(sub, "pytestmark") for sub in _scope_walk([node])):
            continue
        if not (isinstance(node, ast.Assign) and [type(target) for target in node.targets] == [ast.Name]):
            return None
        marks = _marks(node.value, imports)
        if marks is None:
            return None
        found.extend(marks)
    return found


def _scope_collects(body: list[ast.stmt], imports: dict[str, str]) -> bool:
    """No `__test__`, and any `pytestmark` is marks only."""

    for node in _scope_walk(body):
        if _binds(node, "__test__") and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
    return _pytestmarks(body, imports) is not None


def _is_generator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(sub, (ast.Yield, ast.YieldFrom)) for sub in _scope_walk(node.body))


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef, imports: dict[str, str]) -> bool:
    return any((_qualified(decorator, imports) or "").endswith("fixture") for decorator in node.decorator_list)


def _main_guard(node: ast.stmt) -> bool:
    """`if __name__ == "__main__":`, which never runs under pytest."""

    if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1):
        return False
    sides = [node.test.left, *node.test.comparators]
    return isinstance(node.test.ops[0], ast.Eq) and any(
        isinstance(side, ast.Name) and side.id == "__name__" for side in sides
    ) and any(isinstance(side, ast.Constant) and side.value == "__main__" for side in sides)


def _import_time(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Code that runs when the module is imported: module-level statements
    (including a main guard's else branch, though not its body), class bodies
    and annotations; not function bodies."""

    statements: list[ast.stmt] = []
    for node in body:
        statements.extend(node.orelse if _main_guard(node) else [node])
    for node in _scope_walk(statements):
        yield node
        if isinstance(node, ast.ClassDef):
            yield from _import_time(node.body)


def _stores_test_flag(node: ast.AST) -> bool:
    """A store to `__test__` through an attribute, a subscript or setattr,
    whatever the target (`[test_x][0].__test__ = False`)."""

    if isinstance(node, ast.Attribute):
        return node.attr == "__test__" and isinstance(node.ctx, (ast.Store, ast.Del))
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(key, ast.Constant) and key.value == "__test__"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"setattr", "delattr"}:
        return len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "__test__"
    return False


def _module_collects(tree: ast.Module, imports: dict[str, str]) -> bool:
    """Nothing in the module visibly stops pytest collecting it, or rebinds
    names behind the check's back."""

    if not _scope_collects(tree.body, imports):
        return False
    if any(_binds(node, "__getattr__") for node in _scope_walk(tree.body)):
        return False  # a module __getattr__ can answer pytest's __test__ lookup
    if any(_stores_test_flag(node) for node in ast.walk(tree)):
        return False
    every = _every_import(tree)
    for node in _import_time(tree.body):
        if isinstance(node, ast.Raise):
            return False
        if isinstance(node, ast.Call) and _possible(node, every) & _MODULE_STOPS:
            return False
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _REBINDING_BUILTINS:
            return False
        if isinstance(node, (ast.Attribute, ast.Name)) and "sys.modules" in _possible(node, every):
            return False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name.startswith("test")
            and _is_generator(node)
            and not _is_fixture(node, imports)
        ):
            return False  # pytest 9 fails the module: "'yield' keyword is allowed in fixtures, but not in tests"
    return True


def _object_rebound(tree: ast.Module) -> bool:
    """Is the builtin `object`, the one base a test class may name, shadowed
    (`class object: __test__ = False`) or declared global anywhere?"""

    return any(_binds(node, "object") for node in _scope_walk(tree.body)) or any(
        isinstance(node, (ast.Global, ast.Nonlocal)) and "object" in node.names for node in ast.walk(tree)
    )


def _collected_test(node: ast.stmt | None, imports: dict[str, str]) -> ast.FunctionDef | None:
    if (
        isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and all(_collects_with(decorator, imports) for decorator in node.decorator_list)
    ):
        return node
    return None


def _resolve(source: str | bytes, name: str):
    """(tree, imports, function, class or None) for an accepted reference,
    else None."""

    match = TEST_NAME_RE.fullmatch(name)
    if match is None:
        return None
    try:
        tree = ast.parse(source)  # bytes honour a PEP 263 coding cookie
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None
    imports = _imports(tree)
    if not _module_collects(tree, imports):
        return None
    scope = match.group("scope")
    declared_global = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    if {match.group("name"), (scope or "::")[:-2]} & declared_global:
        return None  # a function could rebind it at import time
    if not scope:
        function = _collected_test(_final_definition(tree.body, match.group("name"), imports), imports)
        return (tree, imports, function, None) if function else None
    cls = _final_definition(tree.body, scope[:-2], imports)
    if not (
        isinstance(cls, ast.ClassDef)
        and cls.name.startswith("Test")
        and all(isinstance(base, ast.Name) and base.id == "object" for base in cls.bases)
        and not (cls.bases and _object_rebound(tree))
        and not cls.keywords
        and all(_is_mark(decorator, imports) for decorator in cls.decorator_list)
        and _scope_collects(cls.body, imports)
        and not any(_binds(node, special) for node in _scope_walk(cls.body) for special in ("__init__", "__new__"))
    ):
        return None
    function = _collected_test(_final_definition(cls.body, match.group("name"), imports), imports)
    return (tree, imports, function, cls) if function else None


def python_test_exists(source: str | bytes, name: str) -> bool:
    """Does `name` (test_function, or TestClass::test_method) name a whole test
    that pytest's default collection picks up from this file, by the rules
    above? Whether it then runs is not decided here."""

    return _resolve(source, name) is not None


def _bound_value(values: ast.expr | None, module: list[ast.stmt]) -> ast.expr | None:
    """A parameter set as written, or the value a module-level name is bound
    to once (`CASES = [...]`)."""

    if isinstance(values, ast.Name):
        bound = [
            node.value
            for node in module
            if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [values.id]
        ]
        return bound[0] if len(bound) == 1 else None
    return values


def _empty_values(values: ast.expr | None) -> bool:
    """A parameter set the file shows is empty: `[]`, `()`, `list()`,
    `range(0)`, or a comprehension over one of those."""

    if isinstance(values, (ast.List, ast.Tuple, ast.Set)):
        return not values.elts
    if isinstance(values, ast.Call) and isinstance(values.func, ast.Name) and values.func.id in {"list", "tuple", "range"}:
        args = values.args
        return not args or (values.func.id == "range" and len(args) == 1 and isinstance(args[0], ast.Constant) and args[0].value == 0) or (
            values.func.id != "range" and len(args) == 1 and _empty_values(args[0])
        )
    if isinstance(values, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return any(_empty_values(generator.iter) for generator in values.generators)
    return False


def _mark_skips(node: ast.expr, imports: dict[str, str], module: list[ast.stmt]) -> str | None:
    """Why a mark can stop the test running, if it visibly can."""

    mark = _mark_name(node, imports)
    if mark in _SKIP_MARKS:
        return f"a pytest.mark.{mark} mark"
    if mark != "parametrize" or not isinstance(node, ast.Call):
        return None
    values = _bound_value(
        node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "argvalues"), None),
        module,
    )
    if _empty_values(values):
        return "an empty parameter set"
    if isinstance(values, (ast.List, ast.Tuple)) and values.elts and all(
        isinstance(value, ast.Call)
        and _qualified(value, imports) == "pytest.param"
        and any(
            keyword.arg == "marks"
            and any(_mark_name(item, imports) in _SKIP_MARKS for item in ast.walk(keyword.value) if isinstance(item, ast.expr))
            for keyword in value.keywords
        )
        for value in values.elts
    ):
        return "a skip or xfail mark on every case"
    return None


def _skip_calls(function: ast.AST, every: dict[str, set[str]]) -> list[str]:
    return sorted(
        {name for node in ast.walk(function) if isinstance(node, ast.Call) for name in _possible(node, every) & _SKIP_CALLS}
    )


def _fixtures(body: list[ast.stmt], imports: dict[str, str]) -> dict[str, tuple[ast.FunctionDef, bool]]:
    """Fixture name -> (def, autouse) for the fixtures defined in this body."""

    found = {}
    for node in body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (_qualified(decorator, imports) or "").endswith("fixture"):
                continue
            keywords = {k.arg: k.value for k in decorator.keywords} if isinstance(decorator, ast.Call) else {}
            name = keywords.get("name")
            autouse = keywords.get("autouse")
            found[name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else node.name] = (
                node,
                isinstance(autouse, ast.Constant) and autouse.value is True,
            )
    return found


def _usefixtures(marks: list[ast.expr], imports: dict[str, str]) -> set[str]:
    return {
        arg.value
        for mark in marks
        if isinstance(mark, ast.Call) and _mark_name(mark, imports) == "usefixtures"
        for arg in mark.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }


def python_test_warnings(source: str | bytes, name: str) -> list[str]:
    """Ways this file shows that an accepted test may be collected and still
    not run: skip calls in its body, in same-file fixtures it reaches, in
    setup functions or in same-file functions any of those call; skip, skipif
    or xfail marks (decorated, in pytestmark or applied by a call); an empty
    parameter set; Hypothesis phases or a profile loaded at import. Conftest,
    plugins and ini settings are not read."""

    resolved = _resolve(source, name)
    if resolved is None:
        return []
    tree, imports, function, cls = resolved
    module = tree.body
    marks = list(function.decorator_list)
    marks += _pytestmarks(module, imports) or []
    if cls is not None:
        marks += list(cls.decorator_list) + (_pytestmarks(cls.body, imports) or [])
    targets = {function.name} if cls is None else {cls.name}
    for node in _scope_walk(module):
        if _passes(node, next(iter(targets))) and _is_mark(node, imports, applied=True):
            marks.append(node.func)  # pytest.mark.skip(test_x) applied by a call
    reasons = [reason for reason in (_mark_skips(mark, imports, module) for mark in marks) if reason]

    places = [("its body", function)]
    available = _fixtures(module, imports)
    if cls is not None:
        available.update(_fixtures(cls.body, imports))
    wanted = {arg.arg for arg in function.args.args + function.args.kwonlyargs} - {"self", "cls"}
    wanted |= _usefixtures(marks, imports)
    wanted |= {fixture for fixture, (_, autouse) in available.items() if autouse}
    reached: set[str] = set()
    while wanted - reached:
        fixture = min(wanted - reached)
        reached.add(fixture)
        if fixture in available:
            node = available[fixture][0]
            places.append((f"fixture {fixture}", node))
            wanted |= {arg.arg for arg in node.args.args + node.args.kwonlyargs} - {"self", "cls", "request"}
    setups = [(module, _SETUPS["module"])] + ([(cls.body, _SETUPS["class"])] if cls is not None else [])
    for body, setup_names in setups:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in setup_names:
                places.append((node.name, node))
    # Same-file module-level functions these call, transitively (a data-gated
    # test often skips inside a helper such as `_published_case(...)`).
    helpers = {
        node.name: node for node in module if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_fixture(node, imports)
    }
    queue, called = [node for _, node in places], set()
    while queue:
        for call in ast.walk(queue.pop()):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in helpers:
                if call.func.id not in called and helpers[call.func.id] is not function:
                    called.add(call.func.id)
                    places.append((f"{call.func.id}, which it calls", helpers[call.func.id]))
                    queue.append(helpers[call.func.id])
    every = _every_import(tree)
    for where, node in places:
        reasons.extend(f"{call} in {where}" for call in _skip_calls(node, every))

    for decorator in function.decorator_list:
        if _qualified(decorator, imports) == "hypothesis.settings" and isinstance(decorator, ast.Call):
            keywords = {k.arg: k.value for k in decorator.keywords}
            if "phases" in keywords:
                reasons.append("Hypothesis settings that set phases")
            examples = keywords.get("max_examples")
            if isinstance(examples, ast.Constant) and examples.value == 0:
                reasons.append("Hypothesis max_examples=0")
    for node in _scope_walk(module):
        if isinstance(node, ast.Call) and (_qualified(node, imports) or "").endswith("settings.load_profile"):
            reasons.append("a Hypothesis profile loaded at import")
    return list(dict.fromkeys(reasons))


def missing_test(name: str, where: str) -> str:
    """Why a test reference does not resolve, worded for its kind of file."""

    if where.endswith(".py"):
        return f"{name} is not a test pytest collects from {where} (see tools/README.md, Test references)."
    return f"{name} is not a test case in {where}."


def test_exists(path: str, name: str, data: bytes) -> bool:
    if path.endswith(".py"):
        return python_test_file(path) and python_test_exists(data, name)
    if path.endswith(".yaml"):  # a companion .test.yaml, or another repository's YAML tests
        try:
            return name in yaml_test_case_names(data.decode("utf-8"))
        except UnicodeDecodeError:
            return False
    return False


# ---------------------------------------------------------------------------
# check (offline)


def _walk_strings(value: Any, label: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield label, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{label}[{index}]")


def hygiene_issues(payload: Any) -> list[str]:
    issues: list[str] = []
    for label, text in _walk_strings(payload, "register"):
        # Commit and sha256 fields are hex; skip them so a hash never reads
        # as a decision id.
        if label.endswith((".commit", ".sha256")):
            continue
        for pattern, what in HYGIENE_PATTERNS:
            if pattern.search(text):
                issues.append(f"{label} contains {what}; the register is public.")
    return issues


def _check_reading_ref(value: Any, *, label: str, alternatives: Any, issues: list[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label} must be `{ADOPTED_READING}` or the id of one of this entry's alternatives.")
        return False
    if value != ADOPTED_READING and value not in alternative_ids(alternatives):
        issues.append(
            f"{label} names `{value}`, which is neither `{ADOPTED_READING}` nor the id of one of this "
            "entry's alternatives."
        )
        return False
    return True


def _check_years(years: Any, *, label: str, issues: list[str]) -> None:
    if not _mapping(years, label=label, allowed={"from", "to", "type", "note"}, issues=issues):
        return
    start, end = years.get("from"), years.get("to")
    for key, value, required in (("from", start, True), ("to", end, False)):
        if value is None and not required and key not in years:
            continue
        if type(value) is not int or not MIN_YEAR <= value <= MAX_YEAR:
            issues.append(f"{label}.{key} must be a whole year from {MIN_YEAR} to {MAX_YEAR}.")
    if type(start) is int and type(end) is int and end < start:
        issues.append(f"{label}.to ({end}) is before {label}.from ({start}); both bounds are inclusive.")
    year_type = years.get("type")
    if year_type not in YEAR_TYPES:
        issues.append(f"{label}.type must be one of {', '.join(YEAR_TYPES)}.")
    if "note" in years:
        _string(years.get("note"), label=f"{label}.note", issues=issues)
    elif year_type == "program":
        issues.append(f"{label}.note is required when type is `program`: say what the program year is.")


def _check_unpublished_source(item: Any, *, label: str, issues: list[str]) -> None:
    if not _mapping(item, label=label, allowed={"kind", "description", "date"}, issues=issues):
        return
    for key in ("kind", "description", "date"):
        if key not in item:
            issues.append(f"{label}.{key} is required.")
    if "kind" in item and item["kind"] not in SOURCE_KINDS:
        issues.append(f"{label}.kind must be one of {', '.join(SOURCE_KINDS)}.")
    if "date" in item and not _real_date(item["date"]):
        issues.append(f"{label}.date must be a real YYYY-MM-DD date.")
    description = item.get("description")
    if "description" in item and _string(description, label=f"{label}.description", issues=issues):
        if "@" in description:
            issues.append(f"{label}.description contains `@`; describe the source, never give an address.")
        for quoted in QUOTED_RE.findall(description):
            if len(quoted.split()) > MAX_QUOTED_WORDS:
                issues.append(
                    f"{label}.description quotes more than {MAX_QUOTED_WORDS} words; an unpublished "
                    "source is described, never quoted."
                )
                break


def _check_snapshot(snapshot: Any, *, label: str, issues: list[str]) -> None:
    if not _mapping(snapshot, label=label, allowed={"repo", "commit", "path", "sha256"}, issues=issues):
        return
    for key in ("repo", "commit", "path", "sha256"):
        if key not in snapshot:
            issues.append(f"{label}.{key} is required.")
    if "repo" in snapshot and not (isinstance(snapshot["repo"], str) and REPO_RE.fullmatch(snapshot["repo"])):
        issues.append(f"{label}.repo must be owner/name.")
    if "commit" in snapshot and not (isinstance(snapshot["commit"], str) and COMMIT_RE.fullmatch(snapshot["commit"])):
        issues.append(f"{label}.commit must be a full lowercase commit SHA.")
    if "path" in snapshot and not _relative_path(snapshot["path"]):
        issues.append(f"{label}.path must be a relative path inside the repository.")
    if "sha256" in snapshot and not (isinstance(snapshot["sha256"], str) and SHA256_RE.fullmatch(snapshot["sha256"])):
        issues.append(f"{label}.sha256 must be a lowercase sha256 digest.")


def _check_external_ref(item: Mapping[str, Any], *, label: str, ctx: RepoContext, issues: list[str]) -> bool:
    """repo/commit/path of a reference; returns True when it points outside."""

    repo = item.get("repo")
    if repo is None:
        if "commit" in item:
            issues.append(f"{label}.commit is only for a file in another repository.")
        return False
    if not (isinstance(repo, str) and REPO_RE.fullmatch(repo)):
        issues.append(f"{label}.repo must be owner/name.")
        return True
    if repo == ctx.repo:
        issues.append(f"{label}.repo names this repository; omit repo and commit for a file here.")
        return False
    if not (isinstance(item.get("commit"), str) and COMMIT_RE.fullmatch(item["commit"])):
        issues.append(f"{label}.commit must be a full lowercase commit SHA.")
    return True


def check_entry(entry: Any, *, index: int, ctx: RepoContext, jurisdictions: frozenset[str]) -> list[str]:
    location = f"entries[{index}]"
    entry_id = entry.get("id") if isinstance(entry, dict) else None
    label = f"Entry `{entry_id}` ({location})" if isinstance(entry_id, str) and entry_id.strip() else location
    issues: list[str] = []
    if not _mapping(entry, label=label, allowed=ENTRY_KEYS, issues=issues):
        return issues
    for key in REQUIRED_ENTRY_KEYS:
        if key not in entry:
            issues.append(f"{label}.{key} is required.")
    if isinstance(entry_id, str) and KEBAB_RE.fullmatch(entry_id) is None:
        issues.append(f"{label}.id must match [a-z0-9]+(-[a-z0-9]+)*.")
    elif "id" in entry and not isinstance(entry_id, str):
        issues.append(f"{label}.id must be a string.")
    jurisdiction = entry.get("jurisdiction")
    if "jurisdiction" in entry and not (isinstance(jurisdiction, str) and jurisdiction in jurisdictions):
        issues.append(f"{label}.jurisdiction must be a jurisdiction directory of this repository ({', '.join(sorted(jurisdictions))}).")
    program = entry.get("program")
    if "program" in entry and not (isinstance(program, str) and KEBAB_RE.fullmatch(program)):
        issues.append(f"{label}.program must be kebab-case.")

    pending = entry.get("pending_texts", [])
    has_texts = isinstance(entry.get("texts"), list) and bool(entry.get("texts"))
    has_pending = isinstance(pending, list) and bool(pending)
    issues.extend(check_interpretation_core(entry, label=label, require_texts=False))
    if not has_texts and not has_pending:
        issues.append(f"{label} must cite at least one provision in texts or pending_texts.")

    if "pending_texts" in entry:
        if not _nonempty_list(pending, label=f"{label}.pending_texts", issues=issues):
            pending = []
        for p_index, item in enumerate(pending):
            p_label = f"{label}.pending_texts[{p_index}]"
            if not _mapping(item, label=p_label, allowed={"citation", "kind", "url", "snapshot", "excerpt"}, issues=issues):
                continue
            _string(item.get("citation"), label=f"{p_label}.citation", issues=issues)
            if item.get("kind") not in SOURCE_KINDS:
                issues.append(f"{p_label}.kind must be one of {', '.join(SOURCE_KINDS)}.")
            _string(item.get("excerpt"), label=f"{p_label}.excerpt", issues=issues)
            if not _https_url(item.get("url")):
                issues.append(f"{p_label}.url must be an https URL.")
            _check_snapshot(item.get("snapshot"), label=f"{p_label}.snapshot", issues=issues)

    texts = entry.get("texts") if isinstance(entry.get("texts"), list) else []
    cited_paths = [
        text["corpus_citation_path"]
        for text in texts
        if isinstance(text, dict)
        and isinstance(text.get("corpus_citation_path"), str)
        and CORPUS_PATH_RE.fullmatch(text["corpus_citation_path"])
    ]
    for path in dict.fromkeys(cited_paths):
        if document_class(path) not in DOCUMENT_CLASS_KINDS:
            issues.append(
                f"{label}.texts cites {path}, whose document class `{document_class(path)}` has no "
                "DOCUMENT_CLASS_KINDS row; add one rather than leave its kind open."
            )
    if "text_kinds" in entry:
        overrides = entry["text_kinds"]
        if not isinstance(overrides, dict) or not overrides:
            issues.append(f"{label}.text_kinds must be a non-empty mapping of corpus citation path to kind.")
        else:
            for path, kind in overrides.items():
                k_label = f"{label}.text_kinds[{path}]"
                if path not in cited_paths:
                    issues.append(f"{k_label} names a path that is not one of this entry's texts.")
                    continue
                allowed = DOCUMENT_CLASS_KINDS.get(document_class(path))
                if kind not in SOURCE_KINDS:
                    issues.append(f"{k_label} must be one of {', '.join(SOURCE_KINDS)}.")
                elif allowed is not None and kind not in allowed:
                    issues.append(
                        f"{k_label} is `{kind}`, but a `{document_class(path)}` path can only be "
                        f"{', '.join(allowed)}."
                    )

    years = entry.get("years")
    if "years" in entry:
        _check_years(years, label=f"{label}.years", issues=issues)
    conflict = entry.get("conflict", "live")
    if conflict not in CONFLICT_STATES:
        issues.append(f"{label}.conflict must be live or resolved.")
    elif conflict == "resolved":
        if "resolution" not in entry:
            issues.append(f"{label}.resolution is required when conflict is `resolved`.")
        if not (isinstance(years, dict) and "to" in years):
            issues.append(f"{label}.years.to is required when conflict is `resolved`: the last year the sources disagree.")
    elif "resolution" in entry:
        issues.append(f"{label}.resolution must be absent while conflict is `live`; set conflict: resolved.")
    if "resolution" in entry:
        resolution = entry["resolution"]
        r_label = f"{label}.resolution"
        if _mapping(resolution, label=r_label, allowed={"by", "effective", "record"}, issues=issues):
            if resolution.get("by") not in RESOLVERS:
                issues.append(f"{r_label}.by must be legislation or agency.")
            if not _real_date(resolution.get("effective")):
                issues.append(f"{r_label}.effective must be a real YYYY-MM-DD date.")
            if not _https_url(resolution.get("record")):
                issues.append(f"{r_label}.record must be an https URL to a public record.")
    if "unpublished_sources" in entry:
        unpublished = entry["unpublished_sources"]
        if _nonempty_list(unpublished, label=f"{label}.unpublished_sources", issues=issues):
            for u_index, item in enumerate(unpublished):
                _check_unpublished_source(item, label=f"{label}.unpublished_sources[{u_index}]", issues=issues)

    if "proposed" in entry:
        proposed = entry["proposed"]
        if _nonempty_list(proposed, label=f"{label}.proposed", issues=issues):
            for p_index, item in enumerate(proposed):
                p_label = f"{label}.proposed[{p_index}]"
                if not _mapping(item, label=p_label, allowed={"repo", "pr", "path", "follows"}, issues=issues):
                    continue
                repo = item.get("repo")
                if not (isinstance(repo, str) and REPO_RE.fullmatch(repo)):
                    issues.append(f"{p_label}.repo must be owner/name.")
                pr = item.get("pr")
                match = PR_URL_RE.fullmatch(pr) if isinstance(pr, str) else None
                if match is None:
                    issues.append(f"{p_label}.pr must be a github.com pull request URL.")
                elif isinstance(repo, str) and match.group("repo") != repo:
                    issues.append(f"{p_label}.pr is a pull request in {match.group('repo')}, not {repo}.")
                if not _relative_path(item.get("path")):
                    issues.append(f"{p_label}.path must be a relative path inside the repository.")
                _check_reading_ref(item.get("follows"), label=f"{p_label}.follows", alternatives=entry.get("alternatives"), issues=issues)

    outcome = entry.get("outcome")
    if "outcome" in entry and outcome not in OUTCOMES:
        issues.append(f"{label}.outcome must be computed or refused.")

    alternatives = entry.get("alternatives")
    adopted = entry.get("adopted_by", [])
    implementations = entry.get("implementations", [])
    tests = entry.get("tests", [])

    if "adopted_by" in entry:
        if not _nonempty_list(adopted, label=f"{label}.adopted_by", issues=issues):
            adopted = []
        elif has_pending:
            issues.append(
                f"{label} is adopted by a module but still has pending_texts; a module's "
                "interpretation texts must verify against the corpus, so ingest them first."
            )
        seen_pairs: set[tuple[str, str]] = set()
        for a_index, item in enumerate(adopted):
            a_label = f"{label}.adopted_by[{a_index}]"
            if not _mapping(item, label=a_label, allowed={"module", "interpretation"}, issues=issues):
                continue
            module = item.get("module")
            interp = item.get("interpretation")
            if not (isinstance(module, str) and _relative_path(module) and ctx.module_path_ok(module)):
                issues.append(f"{a_label}.module must be a RuleSpec module path in this repository.")
                continue
            if not (isinstance(interp, str) and KEBAB_RE.fullmatch(interp)):
                issues.append(f"{a_label}.interpretation must be a kebab-case interpretation id.")
                continue
            if (module, interp) in seen_pairs:
                issues.append(f"{a_label} repeats ({module}, {interp}).")
            seen_pairs.add((module, interp))
            issues.extend(_check_adopting_module(entry, module=module, interpretation=interp, label=a_label, ctx=ctx))

    if "implementations" in entry:
        if not _nonempty_list(implementations, label=f"{label}.implementations", issues=issues):
            implementations = []
        for i_index, item in enumerate(implementations):
            i_label = f"{label}.implementations[{i_index}]"
            if not _mapping(
                item,
                label=i_label,
                allowed={"repo", "commit", "path", "follows", "adopts", "basis", "ruling", "cites_entry"},
                issues=issues,
            ):
                continue
            if "repo" not in item:
                issues.append(f"{i_label}.repo is required; a RuleSpec module in this repository goes in adopted_by.")
            else:
                _check_external_ref(item, label=i_label, ctx=ctx, issues=issues)
            if not _relative_path(item.get("path")):
                issues.append(f"{i_label}.path must be a relative path inside the repository.")
            _check_reading_ref(item.get("follows"), label=f"{i_label}.follows", alternatives=alternatives, issues=issues)
            # A consumer may adopt a different reading from the entry's own
            # (PolicyEngine-US follows the statute where the entry adopts an
            # agency's practice); it says why, and may cite its own ruling.
            if "adopts" in item:
                _check_reading_ref(item.get("adopts"), label=f"{i_label}.adopts", alternatives=alternatives, issues=issues)
            if "basis" in item:
                _string(item.get("basis"), label=f"{i_label}.basis", issues=issues)
            elif "adopts" in item or "ruling" in item:
                issues.append(f"{i_label}.basis is required with adopts or ruling: say why this consumer adopts that reading.")
            if "ruling" in item:
                _check_record(item["ruling"], label=f"{i_label}.ruling", confirmation=False, issues=issues)
            if not isinstance(item.get("cites_entry"), bool):
                issues.append(f"{i_label}.cites_entry must be true or false.")

    if "tests" in entry:
        if not _nonempty_list(tests, label=f"{label}.tests", issues=issues):
            tests = []
        seen_tests: set[tuple[Any, Any, Any]] = set()
        for t_index, item in enumerate(tests):
            t_label = f"{label}.tests[{t_index}]"
            if not _mapping(item, label=t_label, allowed={"repo", "commit", "path", "name", "pins"}, issues=issues):
                continue
            external = _check_external_ref(item, label=t_label, ctx=ctx, issues=issues)
            path = item.get("path")
            name = item.get("name")
            # Another repository's YAML test cases (PolicyEngine-US's
            # policy tests) count, pinned like any external test; here, a
            # companion test is always .test.yaml.
            suffixes = (".py", ".yaml") if external else (".py", ".test.yaml")
            if not _relative_path(path) or not str(path).endswith(suffixes):
                issues.append(
                    f"{t_label}.path must be a relative "
                    + (".py or .yaml path." if external else ".py or .test.yaml path.")
                )
                path = None
            elif str(path).endswith(".py") and not python_test_file(path):
                issues.append(
                    f"{t_label}.path {path} is not a test file (test_*.py); only a test can pin a reading."
                )
                path = None
            elif external and str(path).endswith(".yaml") and "tests" not in PurePosixPath(path).parts[:-1]:
                issues.append(
                    f"{t_label}.path {path} is not under a tests directory; another repository's YAML test "
                    "file must be one of its tests."
                )
                path = None
            if isinstance(path, str) and path.endswith(".yaml"):
                if not (isinstance(name, str) and name.strip() and "\n" not in name and name == name.strip()):
                    issues.append(f"{t_label}.name must be a YAML test case's name, on one line.")
                    name = None
            elif isinstance(name, str) and "[" in name:
                issues.append(
                    f"{t_label}.name {name} names a parametrized case; pin the whole test function "
                    "(pytest builds case ids from more than the file shows)."
                )
                name = None
            elif not (isinstance(name, str) and TEST_NAME_RE.fullmatch(name)):
                issues.append(f"{t_label}.name must be a test function, optionally Class::function.")
                name = None
            pins = item.get("pins")
            if not (isinstance(pins, list) and pins):
                issues.append(f"{t_label}.pins must be a non-empty list of `reading` or alternative ids.")
            else:
                for p_index, ref in enumerate(pins):
                    _check_reading_ref(ref, label=f"{t_label}.pins[{p_index}]", alternatives=alternatives, issues=issues)
                if len(set(map(str, pins))) != len(pins):
                    issues.append(f"{t_label}.pins repeats a reading.")
            repo_key = item.get("repo") if isinstance(item.get("repo"), str) else repr(item.get("repo"))
            key = (repo_key, path, name)
            if key in seen_tests and None not in key[1:]:
                issues.append(f"{t_label} repeats {path}::{name}.")
            seen_tests.add(key)
            if not external and path and name:
                local = ctx.root / path
                if not local.is_file():
                    issues.append(f"{t_label}.path {path} does not exist in this repository.")
                elif not test_exists(path, name, local.read_bytes()):
                    issues.append(f"{t_label}.name {missing_test(name, path)}")

    pinned = {
        ref
        for item in tests or []
        if isinstance(item, dict) and isinstance(item.get("pins"), list)
        for ref in item["pins"]
        if isinstance(ref, str)
    }
    if adopted and "reading" not in pinned:
        issues.append(f"{label} is adopted by a module, so at least one test must pin `reading`.")
    for i_index, item in enumerate(implementations or []):
        follows = item.get("follows") if isinstance(item, dict) else None
        if isinstance(follows, str) and (follows == ADOPTED_READING or follows in alternative_ids(alternatives)) and follows not in pinned:
            issues.append(f"{label}.implementations[{i_index}] follows {follows}, which no test pins.")

    if "money_case" in entry:
        money = entry["money_case"]
        m_label = f"{label}.money_case"
        if _mapping(money, label=m_label, allowed={"description", "amount", "currency", "period", "computed_by"}, issues=issues):
            _string(money.get("description"), label=f"{m_label}.description", issues=issues)
            # A public amount needs a pinned test that computes it.
            names = [item.get("name") for item in tests or [] if isinstance(item, dict)]
            if money.get("computed_by") not in names or names.count(money.get("computed_by")) != 1:
                issues.append(
                    f"{m_label}.computed_by must name the one test in this entry's `tests` that computes the "
                    "amount; record no amount that no pinned test computes."
                )
            amount = money.get("amount")
            if not (isinstance(amount, str) and DECIMAL_RE.fullmatch(amount)):
                issues.append(f"{m_label}.amount must be a non-negative decimal string such as \"21.00\".")
            if not (isinstance(money.get("currency"), str) and CURRENCY_RE.fullmatch(money["currency"])):
                issues.append(f"{m_label}.currency must be an ISO 4217 code such as USD.")
            if money.get("period") not in PERIODS:
                issues.append(f"{m_label}.period must be one of {', '.join(PERIODS)}.")
    return issues


def _check_adopting_module(entry: Mapping[str, Any], *, module: str, interpretation: str, label: str, ctx: RepoContext) -> list[str]:
    path = ctx.root / module
    if not path.is_file():
        return [f"{label}.module {module} does not exist."]
    found = module_interpretations(path)
    if found is None:
        return [f"{label}: {module} has no module.interpretations entry `{interpretation}`."]
    entries, rule_names = found
    matches = [item for item in entries if item.get("id") == interpretation]
    if not matches:
        return [f"{label}: {module} has no module.interpretations entry `{interpretation}`."]
    carried = matches[0]
    issues = check_module_interpretation(carried, rule_names=rule_names, label=f"{label}: {module}#{interpretation}")
    for name in INTERPRETATION_CORE:
        if carried.get(name) != entry.get(name):
            issues.append(
                f"{label}: {module}#{interpretation} `{name}` differs from the register entry; "
                "the module must carry the entry unchanged."
            )
    return issues


def check_module_interpretation(item: Any, *, rule_names: set[str], label: str) -> list[str]:
    """One module.interpretations v1 entry, by the rules axiom-encode applies
    (tests/test_source_conflicts_properties.py checks the two agree)."""

    issues: list[str] = []
    if not _mapping(item, label=label, allowed=MODULE_INTERPRETATION_KEYS, issues=issues):
        return issues
    interpretation_id = item.get("id")
    if _string(interpretation_id, label=f"{label}.id", issues=issues) and KEBAB_RE.fullmatch(interpretation_id) is None:
        issues.append(f"{label}.id must match [a-z0-9]+(-[a-z0-9]+)*.")
    issues.extend(check_interpretation_core(item, label=label, require_texts=True))
    affects = item.get("affects")
    if _nonempty_list(affects, label=f"{label}.affects", issues=issues):
        for index, name in enumerate(affects):
            if _string(name, label=f"{label}.affects[{index}]", issues=issues) and name not in rule_names:
                issues.append(f"{label} affects `{name}`, which is not a rule in this module.")
    return issues


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def registered_pairs(payload: Any) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    entries = payload.get("entries") if isinstance(payload, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        adopted = entry.get("adopted_by")
        for item in adopted if isinstance(adopted, list) else []:
            if isinstance(item, dict) and isinstance(item.get("module"), str) and isinstance(item.get("interpretation"), str):
                pairs.add((item["module"], item["interpretation"]))
    return pairs


def unregistered_tensions(root: Path, payload: Any) -> tuple[list[str], list[str]]:
    """(failures, warnings) for module interpretations whose texts cite two or
    more provisions but which no entry adopts. Two provisions in tension are a
    source disagreement.

    A ruled, confirmed or contradicted interpretation fails: it comes from a
    human ruling or a publisher's reply, which is when its register entry is
    written. An open one only warns: signed encoder re-encodes carry the module
    and its manifest alone, and may emit open interpretations, so failing on
    them would block every such re-encode until someone added an entry by hand.
    The source-conflicts workflow lists open ones in a tracking issue.
    """

    pairs = registered_pairs(payload)
    failures: list[str] = []
    warnings: list[str] = []
    for path in iter_module_files(root):
        found = module_interpretations(path)
        if found is None:
            continue
        module = path.relative_to(root).as_posix()
        for item in found[0]:
            texts = item.get("texts")
            citations = {
                text.get("corpus_citation_path")
                for text in texts
                if isinstance(text, dict) and isinstance(text.get("corpus_citation_path"), str)
            } if isinstance(texts, list) else set()
            interp = item.get("id")
            if len(citations) >= 2 and (module, interp) not in pairs:
                message = (
                    f"{module}#{interp} ({item.get('status')}) records provisions in tension "
                    f"({', '.join(sorted(citations))}) but is not in {REGISTER_FILENAME}; "
                    "add an entry whose adopted_by names it."
                )
                (warnings if item.get("status") == "open" else failures).append(message)
    return failures, warnings


def test_pin_warnings(payload: Any, root: Path) -> list[str]:
    """Non-blocking: in-repo Python tests that are collected but that the file
    shows may not run (see python_test_warnings)."""

    warnings = []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        tests = entry.get("tests") if isinstance(entry, dict) else None
        for t_index, item in enumerate(tests if isinstance(tests, list) else []):
            if not isinstance(item, dict) or "repo" in item:
                continue
            path, name = item.get("path"), item.get("name")
            if not (isinstance(path, str) and isinstance(name, str) and path.endswith(".py") and _relative_path(path)):
                continue
            local = root / path
            if local.is_file():
                for reason in python_test_warnings(local.read_bytes(), name):
                    warnings.append(f"entries[{index}].tests[{t_index}] {path}::{name} may not run: {reason}.")
    return warnings


def check_register(payload: Any, ctx: RepoContext, *, scan_modules: bool = True) -> list[str]:
    issues: list[str] = []
    if not isinstance(payload, dict):
        return [f"{REGISTER_FILENAME} must be a mapping with `version` and `entries`."]
    for key in payload:
        if key not in {"version", "entries"}:
            issues.append(f"{REGISTER_FILENAME} has unknown top-level key `{key}`.")
    if payload.get("version") != SCHEMA_VERSION:
        issues.append(f"{REGISTER_FILENAME} version must be {SCHEMA_VERSION}.")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        issues.append(f"{REGISTER_FILENAME} entries must be a list.")
        entries = []
    jurisdictions = ctx.jurisdictions()
    # An entry's identity is its global id, <jurisdiction>:<id>, so two
    # jurisdictions may each have an entry with the same local id.
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        issues.extend(check_entry(entry, index=index, ctx=ctx, jurisdictions=jurisdictions))
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("jurisdiction"), str):
            gid = global_id(entry)
            if gid in seen:
                issues.append(f"entries[{index}] `{gid}` duplicates entries[{seen[gid]}].")
            seen.setdefault(gid, index)
    issues.extend(hygiene_issues(payload))
    if scan_modules:
        issues.extend(unregistered_tensions(ctx.root, payload)[0])
    return issues


# ---------------------------------------------------------------------------
# verify (network)

Fetch = Callable[[str, str, str], bytes]


def github_fetch(token: str | None = None) -> Fetch:
    """Read one file of a public repository at a commit."""

    cache: dict[tuple[str, str, str], bytes] = {}

    def fetch(repo: str, commit: str, path: str) -> bytes:
        key = (repo, commit, path)
        if key not in cache:
            url = (
                f"https://raw.githubusercontent.com/{repo}/{commit}/"
                + urllib.parse.quote(path)
            )
            request = urllib.request.Request(url)
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(request, timeout=60) as response:
                cache[key] = response.read()
        return cache[key]

    return fetch


def snapshot_text(data: bytes, path: str) -> str:
    """The text an excerpt is matched against: markup stripped for HTML/XML."""

    text = data.decode("utf-8")
    if path.endswith((".html", ".htm", ".xml")):
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        # Inline markup sits inside words and sentences ("exception<b>'s</b>",
        # eCFR's <E T="03">), so it goes without a trace; any other tag ends a
        # block and becomes a space.
        text = INLINE_TAG_RE.sub("", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    elif not path.endswith((".txt", ".md")):
        raise ValueError(f"unsupported snapshot type: {path}")
    return re.sub(r"\s+", " ", text).strip()


def encoder_matcher() -> Callable[[str, str], bool]:
    """axiom-encode's proof-atom matcher, the one module validation uses."""

    from axiom_encode.harness.proof_validator import _source_contains_proof_evidence

    def matches(source_text: str, excerpt: str) -> bool:
        return bool(_source_contains_proof_evidence(source_text=source_text, evidence_text=excerpt.strip()))

    return matches


def pinned_corpus_resolver(corpus_path: Path, release: str, content_sha256: str, public_key: str) -> Callable[[str], str | None]:
    """Resolve a citation in the pinned corpus release exactly as validation does."""

    from axiom_encode.corpus_resolver import (
        CorpusSourceNotFoundError,
        LocalCorpusRelease,
        require_canonical_corpus_citation_path,
        resolve_local_corpus_source,
    )

    local = LocalCorpusRelease(name=release, content_sha256=content_sha256, root=corpus_path, public_key=public_key)

    def resolve(citation_path: str) -> str | None:
        require_canonical_corpus_citation_path(citation_path)
        try:
            return resolve_local_corpus_source(citation_path, local).proof_evidence_text
        except CorpusSourceNotFoundError:
            return None

    return resolve


def github_on_default_branch(token: str | None = None) -> Callable[[str, str], bool]:
    """Is a commit reachable from the repository's default branch? (GitHub compare:
    default...commit is `identical` or `behind` exactly when it is.)"""

    cache: dict[tuple[str, str], bool] = {}
    defaults: dict[str, str] = {}

    def get(url: str) -> Any:
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def reachable(repo: str, commit: str) -> bool:
        key = (repo, commit)
        if key not in cache:
            if repo not in defaults:
                defaults[repo] = get(f"https://api.github.com/repos/{repo}")["default_branch"]
            status = get(f"https://api.github.com/repos/{repo}/compare/{defaults[repo]}...{commit}")["status"]
            cache[key] = status in {"identical", "behind"}
        return cache[key]

    return reachable


def verify_register(
    payload: Mapping[str, Any],
    *,
    fetch: Fetch,
    matches: Callable[[str, str], bool],
    resolve: Callable[[str], str | None] | None,
    on_default_branch: Callable[[str, str], bool] | None = None,
) -> tuple[list[str], list[str]]:
    """(issues, report lines). `resolve` None skips corpus texts, reported.

    Every external commit (implementations, tests, snapshots) must be reachable
    from its repository's default branch: the register records shipped code and
    published captures, never a pull request's branch, whose commits a squash
    merge leaves off the default branch. `on_default_branch` None skips that
    check, reported."""

    issues: list[str] = []
    report: list[str] = []

    def shipped(label: str, repo: str, commit: str) -> None:
        if on_default_branch is None:
            return
        try:
            ok = on_default_branch(repo, commit)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            issues.append(f"{label}: cannot tell whether {repo}@{commit[:12]} is on its default branch: {exc}")
            return
        if not ok:
            issues.append(
                f"{label}: {repo}@{commit[:12]} is not on {repo}'s default branch; pin a commit on it "
                "(after a squash merge, the merge commit), or list an unmerged change under proposed."
            )

    if on_default_branch is None:
        report.append("unchecked: default-branch reachability of external commits (no checker given)")
    counts = {"texts": 0, "pending_texts": 0, "implementations": 0, "tests": 0}
    for index, entry in enumerate(payload.get("entries") or []):
        label = f"{global_id(entry)} (entries[{index}])"
        for t_index, text in enumerate(entry.get("texts") or []):
            t_label = f"{label}.texts[{t_index}]"
            path = text["corpus_citation_path"]
            if resolve is None:
                issues.append(f"{t_label}: not verified; no pinned corpus release was given.")
                continue
            try:
                body = resolve(path)
            except Exception as exc:  # noqa: BLE001 - every resolver failure is a finding
                issues.append(f"{t_label}: {path} could not be resolved in the pinned corpus release: {exc}")
                continue
            if body is None:
                issues.append(f"{t_label}: {path} is not in the pinned corpus release.")
            elif not matches(body, text["excerpt"]):
                issues.append(f"{t_label}: the excerpt is not verbatim at {path} in the pinned corpus release.")
            else:
                counts["texts"] += 1
        for p_index, item in enumerate(entry.get("pending_texts") or []):
            p_label = f"{label}.pending_texts[{p_index}]"
            snap = item["snapshot"]
            shipped(p_label, snap["repo"], snap["commit"])
            try:
                data = fetch(snap["repo"], snap["commit"], snap["path"])
            except (urllib.error.URLError, OSError) as exc:
                issues.append(f"{p_label}: cannot read {snap['repo']}@{snap['commit'][:12]}:{snap['path']}: {exc}")
                continue
            actual = hashlib.sha256(data).hexdigest()
            if actual != snap["sha256"]:
                issues.append(f"{p_label}: snapshot sha256 is {actual}, not {snap['sha256']}.")
                continue
            try:
                text = snapshot_text(data, snap["path"])
            except (UnicodeDecodeError, ValueError) as exc:
                issues.append(f"{p_label}: cannot read the snapshot as text: {exc}")
                continue
            if not matches(text, item["excerpt"]):
                issues.append(f"{p_label}: the excerpt is not verbatim in {snap['path']}.")
            else:
                counts["pending_texts"] += 1
        for i_index, item in enumerate(entry.get("implementations") or []):
            i_label = f"{label}.implementations[{i_index}]"
            shipped(i_label, item["repo"], item["commit"])
            try:
                source = fetch(item["repo"], item["commit"], item["path"]).decode("utf-8")
            except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
                issues.append(f"{i_label}: cannot read {item['repo']}@{item['commit'][:12]}:{item['path']}: {exc}")
                continue
            cites = global_id(entry) in source
            if cites != item["cites_entry"]:
                issues.append(
                    f"{i_label}: cites_entry is {str(item['cites_entry']).lower()} but the file "
                    f"{'contains' if cites else 'does not contain'} `{global_id(entry)}`."
                )
            else:
                counts["implementations"] += 1
            if not item["cites_entry"]:
                report.append(f"uncited: {i_label} ({item['repo']}:{item['path']}) does not cite {global_id(entry)} yet")
            adopts = item.get("adopts", ADOPTED_READING)
            if item["follows"] != adopts:
                report.append(
                    f"lags: {i_label} follows {item['follows']}, not the reading it adopts ({adopts})"
                )
        for r_index, item in enumerate(entry.get("proposed") or []):
            report.append(
                f"proposed: {label}.proposed[{r_index}] {item.get('pr')} would implement "
                f"{item.get('follows')} in {item.get('repo')}:{item.get('path')} (not counted until merged)"
            )
        for u_index, item in enumerate(entry.get("unpublished_sources") or []):
            report.append(
                f"unverifiable: {label}.unpublished_sources[{u_index}] is unpublished "
                f"({item.get('kind')}: {item.get('description')}, {item.get('date')}); verify cannot check it"
            )
        for t_index, item in enumerate(entry.get("tests") or []):
            if "repo" not in item:
                continue  # checked offline
            t_label = f"{label}.tests[{t_index}]"
            shipped(t_label, item["repo"], item["commit"])
            try:
                source = fetch(item["repo"], item["commit"], item["path"])
            except (urllib.error.URLError, OSError) as exc:
                issues.append(f"{t_label}: cannot read {item['repo']}@{item['commit'][:12]}:{item['path']}: {exc}")
                continue
            where = f"{item['repo']}@{item['commit'][:12]}:{item['path']}"
            if not test_exists(item["path"], item["name"], source):
                issues.append(f"{t_label}: {missing_test(item['name'], where)}")
            else:
                counts["tests"] += 1
                if item["path"].endswith(".py"):
                    for reason in python_test_warnings(source, item["name"]):
                        report.append(f"may not run: {t_label} {where}::{item['name']}: {reason}")
    report.insert(
        0,
        "verified: "
        + ", ".join(f"{count} {name.replace('_', ' ')}" for name, count in counts.items()),
    )
    return issues, report


# ---------------------------------------------------------------------------
# aggregate


def repo_jurisdiction_ok(repo: str, jurisdiction: str) -> bool:
    """rulespec-us holds us and us-*; rulespec-uk holds uk and uk-*."""

    name = repo.rsplit("/", 1)[-1]
    if not name.startswith("rulespec-"):
        return False
    country = name.removeprefix("rulespec-")
    return jurisdiction == country or jurisdiction.startswith(f"{country}-")


def citation_label(path: Any) -> Any:
    """A reader's citation for a US Code or CFR corpus path; others unchanged.

    us/statute/42/403/f/1 -> 42 U.S.C. 403(f)(1); us/regulation/20/404/435 -> 20 CFR 404.435.
    """

    if not isinstance(path, str):
        return path
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] == "us" and parts[1] == "statute" and parts[2].isdigit():
        return f"{parts[2]} U.S.C. {parts[3]}" + "".join(f"({part})" for part in parts[4:])
    if len(parts) >= 5 and parts[0] == "us" and parts[1] == "regulation" and parts[2].isdigit():
        return f"{parts[2]} CFR {parts[3]}.{parts[4]}" + "".join(f"({part})" for part in parts[5:])
    return path


def aggregate(registers: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """One row per entry across repositories; issues for collisions."""

    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    seen: dict[str, str] = {}
    for repo in sorted(registers):
        payload = registers[repo]
        for entry in (payload or {}).get("entries") or []:
            if not isinstance(entry, dict):
                continue
            gid = global_id(entry)
            if gid in seen:
                issues.append(f"{gid} is in both {seen[gid]} and {repo}.")
                continue
            seen[gid] = repo
            if not repo_jurisdiction_ok(repo, str(entry.get("jurisdiction"))):
                issues.append(f"{repo}: {gid} has a jurisdiction outside that repository.")
            texts = entry.get("texts") or []
            pending = entry.get("pending_texts") or []
            rows.append(
                {
                    "global_id": gid,
                    "repository": repo,
                    "jurisdiction": entry.get("jurisdiction"),
                    "program": entry.get("program"),
                    "question": entry.get("question"),
                    "years": entry.get("years"),
                    "conflict": entry.get("conflict", "live"),
                    "status": entry.get("status"),
                    "outcome": entry.get("outcome"),
                    "reading": entry.get("reading"),
                    "alternatives": [
                        {"id": alt.get("id"), "reading": alt.get("reading")}
                        for alt in _as_list(entry.get("alternatives"))
                        if isinstance(alt, dict)
                    ],
                    "sources": list(
                        dict.fromkeys(
                            [citation_label(text.get("corpus_citation_path")) for text in texts if isinstance(text, dict)]
                            + [item.get("citation") for item in pending if isinstance(item, dict)]
                        )
                    ),
                    "source_kinds": sorted(
                        {
                            kind
                            for kind in [
                                text_kind(entry, text["corpus_citation_path"])
                                for text in texts
                                if isinstance(text, dict) and isinstance(text.get("corpus_citation_path"), str)
                                and CORPUS_PATH_RE.fullmatch(text["corpus_citation_path"])
                            ]
                            + [item.get("kind") for item in pending if isinstance(item, dict)]
                            + [
                                item.get("kind")
                                for item in entry.get("unpublished_sources") or []
                                if isinstance(item, dict)
                            ]
                            if isinstance(kind, str)
                        }
                    ),
                    "ruling": entry.get("ruling"),
                    "adopted_by": entry.get("adopted_by") or [],
                    "implementations": [
                        {
                            **{key: item.get(key) for key in ("repo", "path", "follows")},
                            "adopts": item.get("adopts", ADOPTED_READING),
                        }
                        for item in _as_list(entry.get("implementations"))
                        if isinstance(item, dict)
                    ],
                    "tests": len(entry.get("tests") or []),
                    "money_case": entry.get("money_case"),
                }
            )
    return rows, issues


def _json_default(value: Any) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def render_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Source conflicts across Axiom registers",
        "",
        f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}. Generated by tools/source_conflicts.py aggregate.",
        "",
        "| Global id | Years | Status | Conflict | Outcome | Kinds | Sources | Money case |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        money = row.get("money_case") or {}
        size = f"{money.get('amount')} {money.get('currency')}/{money.get('period')}" if money else ""
        sources = "; ".join(str(source) for source in row["sources"])
        years = row.get("years") or {}
        span = (
            f"{years.get('from')}\u2013{years.get('to', '')} ({years.get('type')})" if isinstance(years, dict) and years else ""
        )
        kinds = ", ".join(row.get("source_kinds") or [])
        lines.append(
            f"| `{row['global_id']}` | {span} | {row['status']} | {row.get('conflict', 'live')} | "
            f"{row['outcome']} | {kinds} | {sources} | {size} |"
        )
    return "\n".join(lines) + "\n"


def discover_registers(token: str | None) -> dict[str, Any]:
    """Every active rulespec-<country> repository's register (absent = none)."""

    registers: dict[str, Any] = {}
    page = 1
    names: list[tuple[str, str]] = []
    while True:
        url = f"https://api.github.com/orgs/{ORGANISATION}/repos?per_page=100&page={page}&type=public"
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=60) as response:
            batch = json.load(response)
        if not batch:
            break
        names.extend(
            (repo["full_name"], repo["default_branch"])
            for repo in batch
            if not repo.get("archived") and re.fullmatch(r"rulespec-[a-z]{2}(-[a-z0-9]+)*", repo["name"])
        )
        page += 1
    for full_name, branch in sorted(names):
        url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{REGISTER_FILENAME}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                registers[full_name] = yaml.safe_load(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    return registers


# ---------------------------------------------------------------------------
# CLI


def _toolchain_pin(root: Path) -> tuple[str, str]:
    import tomllib

    table = tomllib.loads((root / ".axiom" / "toolchain.toml").read_text())["toolchain"]
    return table["axiom_corpus_release"], table["axiom_corpus_release_content_sha256"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="offline schema and in-repo reference checks")
    check.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    check.add_argument("--repo", default=f"{ORGANISATION}/rulespec-us")
    check.add_argument(
        "--warnings-file",
        type=Path,
        help="also write the warnings here, one per line: unregistered open interpretations and pinned tests that may not run",
    )
    verify = sub.add_parser("verify", help="network checks: excerpts, snapshots, external refs")
    verify.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    verify.add_argument("--corpus-path", type=Path, help="axiom-corpus checkout holding the pinned release")
    verify.add_argument("--public-key", default=os.environ.get("AXIOM_CORPUS_RELEASE_PUBLIC_KEY"))
    agg = sub.add_parser("aggregate", help="collect every rulespec repository's register")
    agg.add_argument("--register", action="append", default=[], metavar="OWNER/REPO=PATH", help="use a local file instead of discovery (repeatable)")
    agg.add_argument("--json", type=Path, help="write the rows as JSON")
    agg.add_argument("--markdown", type=Path, help="write a Markdown table")
    args = parser.parse_args(argv)

    if args.command == "check":
        path = args.root / REGISTER_FILENAME
        try:
            payload = load_register(path)
        except RegisterError as exc:
            print(f"source-conflicts: {exc}", file=sys.stderr)
            return 1
        issues = check_register(payload, RepoContext(root=args.root, repo=args.repo))
        warnings = unregistered_tensions(args.root, payload)[1] if isinstance(payload, dict) else []
        pin_warnings = test_pin_warnings(payload, args.root)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        for warning in warnings + pin_warnings:
            print(f"warning: {warning}")
        if args.warnings_file is not None:
            args.warnings_file.write_text("".join(f"{warning}\n" for warning in warnings + pin_warnings))
        entries = payload.get("entries") if isinstance(payload, dict) else None
        count = len(entries) if isinstance(entries, list) else 0
        print(
            f"{REGISTER_FILENAME}: {count} entr{'y' if count == 1 else 'ies'}, {len(issues)} issue(s), "
            f"{len(warnings)} unregistered open interpretation(s), {len(pin_warnings)} pinned test(s) that may not run."
        )
        return 1 if issues else 0

    if args.command == "verify":
        payload = load_register(args.root / REGISTER_FILENAME)
        try:
            matches = encoder_matcher()
        except ImportError as exc:
            print(f"source-conflicts verify needs axiom-encode importable: {exc}", file=sys.stderr)
            return 2
        resolve = None
        if args.corpus_path is not None:
            if not args.public_key:
                print("source-conflicts verify: --public-key (or AXIOM_CORPUS_RELEASE_PUBLIC_KEY) is required with --corpus-path", file=sys.stderr)
                return 2
            release, content_sha256 = _toolchain_pin(args.root)
            resolve = pinned_corpus_resolver(args.corpus_path, release, content_sha256, args.public_key)
        token = os.environ.get("GITHUB_TOKEN")
        issues, report = verify_register(
            payload,
            fetch=github_fetch(token),
            matches=matches,
            resolve=resolve,
            on_default_branch=github_on_default_branch(token),
        )
        for line in report:
            print(line)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        print(f"{len(issues)} issue(s).")
        return 1 if issues else 0

    token = os.environ.get("GITHUB_TOKEN")
    if args.register:
        registers = {}
        for spec in args.register:
            repo, _, path = spec.partition("=")
            registers[repo] = load_register(Path(path))
    else:
        registers = discover_registers(token)
    rows, issues = aggregate(registers)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2, default=_json_default) + "\n")
    if args.markdown:
        args.markdown.write_text(render_markdown(rows))
    if not args.json and not args.markdown:
        print(json.dumps(rows, indent=2, default=_json_default))
    for issue in issues:
        print(f"- {issue}", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
