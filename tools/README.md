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

## Test references

A test pins a reading only if it runs. The check reads the test file alone, so
it accepts one narrow shape and refuses everything else:

- a `test_*.py` file, and a `test_*` function at module level or in a `Test*`
  class at module level. These are the names every common pytest
  configuration collects; the repository's `python_files`, `python_functions`
  and `python_classes` settings are not read;
- a plain `def`: not `async` (it fails without a plugin) and not a generator;
- the final binding of its name: nothing later in the file rebinds, deletes,
  or sets attributes on the function or its class;
- decorators only from pytest marks other than `skip`, `skipif` and `xfail`,
  Hypothesis (`given`, `settings`, `example`, `seed`) and
  `unittest.mock.patch`. A fixture, `property`, `staticmethod`, `dataclass` or
  any other wrapper is refused, because it can stop pytest collecting the
  function. A `parametrize` whose case list is empty, or whose every case
  carries marks, is refused;
- a class with no bases other than `object`, no metaclass, no `__init__`,
  `__new__` or `__test__`, and only pytest marks as decorators;
- a module with no `__test__`, no `pytestmark` other than a list of marks
  that run, no `pytest.skip`/`importorskip`/`xfail`/`fail`/`exit` call or
  `raise` outside `if __name__ == "__main__":`, and no generator test anywhere
  (pytest 9 rejects the whole module).

A parametrized case (`test_x[case-1]`) cannot be pinned. pytest builds case
ids from more than the file shows (fixture `params`, `pytestmark`,
`pytest_generate_tests`, base classes, conftest hooks) and its escaping rules
change between versions. Pin the whole function; it runs every case.

What the file cannot show is not checked: import errors, conftest hooks,
plugins, ini options, and other tests in the module that fail to collect. Each
of these makes the owning repository's own test run fail loudly, while every
shape above would let a pinned test stop running silently.
`test_resolver_accepts_exactly_what_pytest_runs` checks these rules in both
directions against pytest runs of generated modules.

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
| `tests` | Tests that pin readings: `path`, `name` and `pins`, a list of reading references. Here, `name` is a test function (or `Class::function`) in a `.py` file, or a case name in a companion `.test.yaml`. A test in another repository adds `repo` and `commit`. It may also be a YAML test file (`.yaml`) whose `name` is the case's name, on one line, such as a PolicyEngine-US policy test. Verify checks that the case still exists at that commit. A Python test must be a whole test function that pytest collects and runs; see [Test references](#test-references). Another repository's YAML test file must sit under a `tests` directory. |
| `money_case` | An invented case showing the size of the difference: `description`, `amount` (a decimal string), `currency` and `period` (once, week, month or year). |

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

The register is public. It carries no people's names, decision-queue ids,
session ids, local paths or email addresses. `ruling.by` and `confirmation.by`
name organisations, and `ruling.record` is a public PR, issue or comment.

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
- a hygiene hit;
- an adopting module that is missing, or that does not carry the entry's
  interpretation core unchanged, or whose `affects` name no rule in the module;
- an in-repo test that does not exist, or is not a test pytest collects and runs
  (see [Test references](#test-references));
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
- external implementations, tests and snapshots are pinned to commits reachable from their repository's default branch (a squash-merged pull request's own commits are not), and they exist there; an external Python test must be a test pytest collects and runs there, by the same rules;
- each `cites_entry` claim matches its file.

Both excerpt checks use axiom-encode's own resolver and proof-atom matcher at
`axiom_encode_ref`. The verify report lists:
- implementations that do not cite their entry yet;
- implementations that lag (they follow a reading other than the one they adopt);
- unpublished sources, which it cannot check.

The workflow is not a required check; a red scheduled or main run opens an
alarm issue. `tests/test_source_conflicts_properties.py` (Hypothesis) states
the tool's invariants, including a differential test against axiom-encode's
`_validate_module_interpretations`, which runs once the pinned encoder
accepts module interpretations with alternative ids (encode#1835,
engine#247), and a two-way differential of the test-reference rules against
what pytest actually runs.

Run locally:

```bash
python tools/source_conflicts.py check
AXIOM_CORPUS_RELEASE_PUBLIC_KEY=... python tools/source_conflicts.py verify \
  --corpus-path ../axiom-corpus          # needs axiom-encode importable
python tools/source_conflicts.py aggregate --markdown /tmp/conflicts.md
```
