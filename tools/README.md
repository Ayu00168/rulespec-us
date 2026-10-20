# Program Artifact Pipeline

`programs/` specs are source; compiled artifacts are build outputs and are
never committed. The `program-artifacts` workflow compiles every spec through
axiom-compose + axiom-rules-engine (pinned refs in the workflow env):

- **On PRs** it is a gate: any spec outside `tools/known-broken-specs.txt`
  that fails to compile fails the check, so spec/corpus drift (renamed module
  paths, coverage-gate violations) is caught at PR time.
- **On main** it publishes `dist/` — provenance-stamped `*.compiled.json`,
  composed `*.rulespec.yaml`, and `manifest.json` — as a GitHub release
  tagged `program-artifacts-<shortsha>`.

Every artifact's `metadata.provenance` records the corpus SHA, spec path +
sha256, composer version, and engine version; the manifest carries the same
plus artifact sha256s. Given a corpus commit and the pinned toolchain, the
build is byte-reproducible.

Consumers (axiom-api) pin a release tag and per-file sha256s instead of
vendoring artifacts. To admit a program: land its spec here, wait for the
release, then bump the consumer's pin.

The workflow also signs GitHub build-provenance attestations for
`manifest.json` and every `*.compiled.json`. Consumers can verify a
downloaded artifact was built by this workflow from this repo:

```bash
gh attestation verify manifest.json --repo TheAxiomFoundation/rulespec-us
```

Run locally:

```bash
AXIOM_RULES_ENGINE_BIN=~/axiom-rules/target/release/axiom-rules-engine \
  python tools/build_program_artifacts.py --check   # gate mode
  python tools/build_program_artifacts.py           # writes dist/
```

# Engine root load

The `engine-root-load` workflow asks the question issue #1354 showed nothing
else asked: does `axiom-rules-engine`, at `axiom_rules_engine_ref` in
`.axiom/workflow-toolchain.toml`, load this checkout as a corpus root?
Repository Checks skips every module with an active known-validation-gaps
waiver, so it stayed green while hundreds of modules declared fields that
engine had removed.

`tools/engine_root_load.py` writes a small Rust harness into the pinned engine
checkout as a Cargo example, builds it with `--locked`, admits the checkout
once as a `CanonicalRuleSpecRoots` root, and compiles every atomic module
under `<jurisdiction>/{legislation,policies,regulations,statutes}/` through
`CompiledProgramArtifact::from_rulespec_file`, the entry point behind
`axiom-rules-engine compile`. A stored `module.kind: composition` module, which
the engine refuses on that surface, is also compiled from a copy outside the
checkout through `from_composed_rulespec_file`.

Failures must equal `tools/known-engine-load-failures.txt`
(`<surface>\t<class>\t<module>`) exactly:

- a module that fails and is not listed fails the check;
- a listed module that no longer fails that way fails the check, so a fix
  deletes its line in the same change;
- when the protected base already has a baseline and the engine pin is
  unchanged, a pull request may only shrink the failing `(surface, module)`
  set. A listed module may change class: the engine reports only a module's
  first error, so fixing one can expose another;
- when the protected base has no baseline, the check establishes the first
  baseline without enforcing that shrinking rule. Moving
  `axiom_rules_engine_ref` also permits baseline growth: a newer engine may
  reject more, and the pin bump is where those failures are listed and reviewed.

Run locally from a checkout whose directory is named exactly `rulespec-us`:

```bash
python tools/engine_root_load.py run --engine-src ../axiom-rules-engine \
  --output /tmp/engine-root-load.jsonl
python tools/engine_root_load.py check --results /tmp/engine-root-load.jsonl \
  --base-ref origin/main
python tools/engine_root_load.py baseline --results /tmp/engine-root-load.jsonl \
  > tools/known-engine-load-failures.txt   # after deliberately fixing modules
```

# Source-conflict register

`known-source-conflicts.yaml` records the places where a jurisdiction's
sources disagree, the readings each side supports, and the reading adopted.
Disagreements include a statute against a regulation, a regulation against an
agency manual, and one manual section against another. Every rulespec
repository keeps its own register at its root. The Axiom-wide view is
generated from them with `tools/source_conflicts.py aggregate`; nothing keeps
a second copy.

## Entry shape

An entry's interpretation core is a RuleSpec `module.interpretations` v1
entry: `question`, `texts`, `reading`, `alternatives`, `basis`, `status`,
`ruling` and `confirmation`, with the same field names and status rules.
`open` forbids `ruling` and `confirmation`; `ruled` requires `ruling`;
`confirmed` and `contradicted` require both, and `contradicted` is always a
finding. So when the provision is encoded, the entry moves into the module
unchanged and the module adds `affects`. The register then names that module
under `adopted_by`. The global id of an entry is `<jurisdiction>:<id>`, for
example `us:oasdi-family-maximum-saving-clause`; code outside RuleSpec cites
that id, as `# Source conflict <global id>` at the line that decides the
reading.

Every alternative has an `id`: kebab-case, unique within the entry, and never
`reading`. A reading reference (`follows`, `adopts`, `pins`) is `reading`,
meaning the entry's adopted reading, or an alternative's id. References never
use positions, so inserting or reordering an alternative cannot move anyone's
pick. The `id` is part of the interpretation core: engine#247 and encode#1835
add it to `module.interpretations` before either lands.

Register-only fields:

| Field | Meaning |
|---|---|
| `jurisdiction`, `program` | A jurisdiction directory of this repository, and a kebab-case program slug. |
| `pending_texts` | Provisions the pinned corpus release lacks. Each gives `citation`, its `kind` (see below), `url`, an `excerpt`, and a `snapshot`: repo, commit, path and sha256 of a captured copy (`.txt`, `.md`, `.html`, `.htm` or `.xml`). An entry that a module adopts cannot have any; ingest them first. |
| `text_kinds` | Optional. Narrows the kind of a corpus text, as `{corpus citation path: kind}`. A text's kind is otherwise the first kind its document class allows (below). The kind must be one its path's class allows, so a `/statute/` path is always `statute`. |
| `unpublished_sources` | Sources with no public text, such as an agency's correspondence: `kind`, `description` and `date`. They are described, never quoted: a description holds no `@` and quotes at most four words. Verify cannot check them and lists them as unverifiable. |
| `years` | Optional. The years the sources disagree: `from`, and optionally `to` (both inclusive; no `to` means still running). `type` says which year: `calendar`, `tax`, `federal-fiscal`, `state-fiscal` or `program`. `program` also needs `note`, saying what the program year is. |
| `conflict`, `resolution` | `conflict` is `live` (the default) or `resolved`. `resolved` means a legislature or agency has closed the disagreement. It needs a `resolution` (`by`: `legislation` or `agency`; `effective`, a date; `record`, a public https URL) and `years.to`, the last year the sources disagree. A `live` entry has no `resolution`. |
| `outcome` | `computed` or `refused`: whether the adopted reading computes the contested cases or refuses them. For a refusal, `reading` says what is refused and the substantive readings go in `alternatives`. |
| `adopted_by` | RuleSpec modules in this repository that carry the entry, as `module` (path) and `interpretation` (id) pairs. |
| `implementations` | Code outside RuleSpec that takes a reading: `repo`, `commit`, `path`, `follows` (what the code does today) and `cites_entry`, which says whether the file contains the global id. A consumer may adopt a reading other than the entry's: `adopts` (default `reading`) names the reading it intends, `basis` says why, and an optional `ruling` (`by`, `date`, public `record`) is the consumer's own ruling. PolicyEngine-US follows the statute where the entry adopts an agency's practice. An implementation lags when `follows` differs from `adopts`. |
| `proposed` | Changes not yet merged that would implement a reading: `repo`, `pr` (the pull request URL), `path` and `follows`. Verify lists them but never counts them as implementations; move one to `implementations` once it merges, pinned to the commit on the default branch. |
| `tests` | Tests that pin readings: `path`, `name` and `pins`, a list of reading references. Here, `name` is a test function (or `Class::function`) in a `.py` file, or a case name in a companion `.test.yaml`. A test in another repository adds `repo` and `commit`. It may also be a YAML test file (`.yaml`) whose `name` is the case's name, on one line, such as a PolicyEngine-US policy test. Verify checks that the case still exists at that commit. A Python test must be a whole test function that pytest's default collection picks up from the file; whether it runs is the owning repository's CI's business. See [Test references](#test-references). Another repository's YAML test file must sit under a `tests` directory. |
| `money_case` | Optional. An invented case showing the size of the difference: `description`, `amount` (a decimal string), `currency`, `period` (once, week, month or year) and `computed_by`, the name of the one test in the entry's `tests` that computes the amount, asserting it or both figures whose difference it is. A public file carries no amount that no pinned test computes: leave the money case out instead. `check` also requires every `$` amount in the entry's own prose (`question`, `reading`, `basis`, each alternative's `reading` and `effect`) to be one the money case states, so an entry without one has no dollar figures. Money is written one way, `$` and digits with commas between groups of three and optional two-digit cents (`$1,234.56`); other spellings (`USD 200`, `200 dollars`, `$ 200`, `$.50`, `$1.5k`) are issues. |

Source kinds are `statute`, `regulation`, `rulemaking`, `form`,
`instructions`, `worksheet`, `manual`, `guidance`, `policy`, `plan`,
`calculator` and `agency-practice`. `DOCUMENT_CLASS_KINDS` in
`tools/source_conflicts.py` maps each corpus document class to the kinds it
allows; the first is the default:

| Document class | Kinds |
|---|---|
| `statute` | statute |
| `regulation` | regulation |
| `rulemaking` | rulemaking |
| `form` | form, instructions, worksheet |
| `guidance` | guidance, instructions, calculator, plan |
| `manual` | manual |
| `policy` | policy, manual, guidance, plan |
| `district-plan` | plan |

A text whose class has no row is a finding, so a new corpus class fails
closed until it gets one.

The register is public. These publication rules are reviewed by hand: it
carries no people's names; `ruling.by` and `confirmation.by` name
organisations; `ruling.record` is a public PR, issue or comment; and nothing
private is quoted. `check` automates only part of that: `by` must be
non-blank and `record` an https URL, and every string must be free of four
token patterns (decision-queue ids such as `d1234`, session ids, local paths
and email addresses). It cannot tell a person's name from an organisation's,
or a private page from a public one. The file may not use YAML anchors or
aliases; `load_register` refuses them, so no entry can contain itself.

## Checks

`tools/source_conflicts.py check` runs offline. `tests/test_source_conflicts.py`
runs it inside the required Repository Checks pytest leg, and it fails on any
of these:

- a malformed entry, an unknown key or a broken status rule;
- an alternative without an id, or a reading reference (`follows`, `adopts`,
  `pins`) that is neither `reading` nor one of the entry's alternative ids;
- a text whose document class has no `DOCUMENT_CLASS_KINDS` row, or a kind
  its class does not allow;
- a `resolution` without `conflict: resolved`, or a resolved entry without a
  `resolution` or `years.to`;
- a hygiene hit (one of the four token patterns below);
- an adopting module that is missing, or that does not carry the entry's
  interpretation core unchanged, or whose `affects` name no rule in the module;
- an in-repo test that does not exist, or is not a test pytest collects
  (see [Test references](#test-references)). A pinned test that the file
  shows may not run (a skip, xfail, empty parameter set or Hypothesis phases)
  is a warning, not a failure;
- a followed reading that no test pins, or a module adopter whose `reading`
  no test pins;
- a ruled, confirmed or contradicted module interpretation whose texts cite
  two or more provisions but which no entry adopts. Provisions in tension are
  a source disagreement, so they belong in the register. An unregistered `open`
  one is only a warning, because signed encoder re-encodes carry the module and
  its manifest alone and may emit open interpretations. The `source-conflicts`
  workflow lists those warnings in a tracking issue. Its entry is written when
  a ruling or a publisher's reply arrives.

`tools/source_conflicts.py verify` needs the network and runs in the
`source-conflicts` workflow on pull requests touching the register, on pushes
to main, daily and on dispatch. It checks:

- every `texts` excerpt is verbatim at its citation in the pinned corpus release
  from `.axiom/toolchain.toml`;
- every `pending_texts` excerpt is verbatim in its snapshot, whose sha256 must
  match;
- external implementations, tests and snapshots are pinned to commits reachable from their repository's default branch (a squash-merged pull request's own commits are not), and they exist there; an external Python test must be a test pytest collects there, by the same rules, and the report lists external tests the file shows may not run;
- each `cites_entry` claim matches its file.

Both excerpt checks use axiom-encode's own resolver and proof-atom matcher at
`axiom_encode_ref`. The verify report lists:
- implementations that do not cite their entry yet;
- implementations that lag (they follow a reading other than the one they adopt);
- unpublished sources, which it cannot check;
- external tests the file shows may not run.

The workflow is not a required check; a red scheduled or main run opens an
alarm issue. `tests/test_source_conflicts_properties.py` (Hypothesis) states
the tool's invariants, including a differential test against axiom-encode's
`_validate_module_interpretations`, which runs once the pinned encoder
accepts module interpretations with alternative ids (encode#1835,
engine#247), and a two-way differential of the test-reference rules against
what pytest collects. The `source-conflicts` workflow is the gate for these
properties: it installs Hypothesis and the pinned pytest. Repository Checks'
shared workflow runs the suite without Hypothesis, so the properties skip
there.

Run locally:

```bash
python tools/source_conflicts.py check
AXIOM_CORPUS_RELEASE_PUBLIC_KEY=... python tools/source_conflicts.py verify \
  --corpus-path ../axiom-corpus          # needs axiom-encode importable
python tools/source_conflicts.py aggregate --markdown /tmp/conflicts.md
```

## Test references

The check screens a pinned Python test for one thing: that pytest's default
collection picks it up from the test file alone, at the pinned commit. The
guarantee is **accepted and unwarned ⇒ collected**, for a file whose
repository adds no conftest or ini effects and that contains no deliberate
evasion. Whether the test then runs and passes belongs to the owning
repository's CI. The screen catches the ways an honest test file stops a test
being collected. It
is not a proof against deliberately obfuscated import-time code (another
module's helper that rebinds this one's names, `setattr` with a computed
name, and the like); that is out of scope, and such findings go in the
limits below rather than into new rules. It reads only that file, so it
accepts one narrow shape and refuses everything else:

- a `test_*.py` file, and a `test_*` function at module level or in a `Test*`
  class at module level. These are the names every common pytest
  configuration collects; the repository's `python_files`, `python_functions`
  and `python_classes` settings are not read;
- a plain `def`: not `async` (it fails without a plugin) and not a generator;
- the final binding of its name: nothing later in the file rebinds, deletes
  or sets attributes on the function or its class, or passes it to a call
  other than a pytest mark;
- decorators only from pytest marks, Hypothesis (`given`, `settings`,
  `example`, `seed`) and `unittest.mock.patch`, through imports bound nowhere
  else (no `global`, no store under the name such as `pytest.mark.slow =
  ...`). A fixture, `property`, `staticmethod`, `dataclass` or any other
  wrapper is refused, because it can stop pytest collecting the function;
- a class with no bases other than `object`, no metaclass, no `__init__`,
  `__new__` or `__test__`, and only pytest marks as decorators;
- a module with no `__test__` (and no store to `__test__` in any form, such
  as `[test_x][0].__test__ = False`), no module `__getattr__`, no
  `pytestmark` other than marks, no shadowed `object` when a class names it as
  a base, and no generator test anywhere (pytest 9 rejects the whole module);
- no `pytest.skip`/`importorskip`/`xfail`/`fail`/`exit` call, `exec`,
  `eval`, `globals()`, `vars()`, `sys.modules` or
  `importlib.import_module(__name__)` in code that runs at import, and no
  `raise` in it directly. Code that runs at import means module-level
  statements, the `else` branch of an `if __name__ == "__main__":` guard (not
  its body), class bodies, annotations (which run at import before Python
  3.14), and the same-file top-level functions any of that calls or uses as
  a decorator, followed transitively, so an optional-data helper that skips
  the module is refused. A same-file helper may raise an ordinary error,
  which fails loudly, but not a skip exception. Simple aliases such as
  `skip = pytest.skip` resolve to what they name. Blocks that never run,
  `if False:` and `if TYPE_CHECKING:`, are skipped (their `else` runs).

A parametrized case (`test_x[case-1]`) cannot be pinned. pytest builds case
ids from more than the file shows (fixture `params`, `pytestmark`,
`pytest_generate_tests`, base classes, conftest hooks) and its escaping rules
change between versions. Pin the whole function.

**Import-time code is an allowlist.** Every call that runs at import must
resolve to a known-safe callable or to a same-file top-level function the
screen follows and finds clean. Anything else gets a warning: "import-time
code the screen can't follow: `<callee>` at line N; the test may not be
collected". That covers calls into project code, `functools.partial`, an
alias the screen cannot resolve, and a helper bound outside the top level.
The known-safe callables are:
- pytest marks, `pytest.param`, `pytest.fixture` and `pytest.approx`;
- Hypothesis strategies, `settings` (and its methods), `given`, `example`
  and `seed`; calling a `@composite` strategy runs nothing at import;
- `re.compile`, `pathlib` paths, `os.path.*`, `os.environ.get` and
  `os.getenv`;
- `typing`, `dataclasses`, `enum`, `collections` (`namedtuple`,
  `defaultdict`, `OrderedDict`, `Counter`), `itertools`, `math`, `operator`,
  `string` and `textwrap.dedent`;
- `fractions.Fraction`, `decimal.Decimal`, `datetime` dates and times,
  `json.loads` and `json.dumps`;
- `functools` decorators (`lru_cache`, `cache`, `wraps`, `total_ordering`)
  and `unittest.mock.patch`;
- pure builtins (`len`, `range`, `dict`, `list`, `tuple`, `set`,
  `frozenset`, `sorted`, `int`, `float`, `str`, `bool`, `min`, `max`, `sum`,
  `zip`, `enumerate` and the like);
- any method of a value the screen can see is plain data: a literal, a safe
  call's result, or a name bound once to one, such as
  `Path(__file__).parent`.

Warnings are information, not failures. A test whose module builds project
objects at import warns and still runs in its repository's CI. Of the 18
Python pins in the register today, 12 warn for that reason: their modules
call project code such as `g.YearMonth(...)` or `ssa_params()` at import.

**A collected test can still not run, and nothing fails.** `check` (for tests
here) and `verify` (for tests elsewhere) also warn when the file shows one of
these: a `pytest.skip`, `importorskip` or `xfail` call in the test, in a
same-file fixture it reaches (by argument, `usefixtures` or `autouse`), in
`setup_module`, `setup_function`, `setup_class` or `setup_method`, or in a
same-file module-level function any of those call; a `skip`,
`skipif` or `xfail` mark, however applied; an empty parameter set, or one
whose every case carries such a mark; Hypothesis `settings` that set `phases`
or `max_examples=0`, or a profile loaded at import. These are warnings, not
failures: a data-gated test that skips locally can run in its repository's
CI.

Known limits. The register does not check, and cannot see:
- conftest fixtures and hooks (`pytest_collection_modifyitems`,
  `pytest_runtest_setup`, `collect_ignore`, `pytest_ignore_collect`);
- plugins;
- ini or `addopts` selection (`-m`, `-k`, `--deselect`, `--ignore`,
  `testpaths`, `norecursedirs`, `empty_parameter_set_mark`);
- deliberately obfuscated import-time code: rebinding through another
  module's helper, `getattr`/`setattr` with computed names, `eval` of built
  strings reached indirectly, homoglyph names, and similar.

Any of these can stop a pinned test being collected or run while every run
exits 0. Import errors and modules that fail to collect do fail loudly.

The evidence for the screen is `test_resolver_accepts_exactly_what_pytest_collects`
(property 11). It runs pytest on generated modules, next to a stand-in for
project code, and checks four things:
- accepted and unwarned ⇒ collected;
- collected ⇒ accepted, except the deliberate refusals listed in
  `REFUSED_BUT_COLLECTED`;
- accepted and (not collected or not run) ⇒ warned;
- every visible skip the generator plants is warned.

A warning may also fire on a test that runs.

The generator plants every shape that the reviews of this tool found in
honest-looking files. The unit tests in `test_python_test_resolution` and
`test_python_test_warnings` pin each of those shapes.
