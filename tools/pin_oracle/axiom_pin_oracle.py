"""pytest plugin that tools/pin_runner.py loads into a pinned repository's run.

It records what pytest itself did, under the repository's own ini, conftest
files and addopts: every collected item, every item the configuration
deselected, collection errors and module-level skips, and each phase report
of the pinned items. Items are identified by their position in the
collection (`index`), not by node id: a YAML test runner such as
policyengine-core's gives every case in a file the same node id (`path::`),
because a case's name is set only when it runs. A report belongs to the item
`pytest_runtest_protocol` is running when it arrives. tools/pin_runner.py classifies the record; nothing here
decides whether a pin passed. Items the register does not pin are dropped
after the repository's own selection has run, so only pinned tests execute.
For a pinned YAML case it also records the case's own input keys, stated
output, reforms, extensions and error margins (`case`), from which the runner
checks a household choice's optimum.

Environment (set by the runner):
- AXIOM_PIN_ORACLE_OUT: where to write the JSON record.
- AXIOM_PIN_ORACLE_ROOT: the checkout root; paths are recorded relative to it.
- AXIOM_PIN_ORACLE_PINS: JSON list of [path, name] pairs, name being
  `function` or `Class::function` for a Python test, or a YAML case's `name`.

It imports nothing but pytest and the standard library.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_MESSAGE_LIMIT = 2000
_CASE_LIMIT = 200_000  # characters of a case's recorded output


def _input_keys(value: object) -> list[str]:
    """Every mapping key anywhere in a case's input, as strings."""

    keys: set[str] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            keys.update(str(key) for key in node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(keys)


def _jsonable(value: object) -> object:
    """The case's output as JSON data: mapping keys become strings, and any
    other value JSON can't hold becomes its text."""

    def convert(node: object) -> object:
        if isinstance(node, dict):
            return {str(key): convert(child) for key, child in node.items()}
        if isinstance(node, list):
            return [convert(child) for child in node]
        if node is None or isinstance(node, (bool, int, float, str)):
            return node
        return str(node)

    try:
        converted = convert(value)
        return converted if len(json.dumps(converted)) <= _CASE_LIMIT else None
    except (RecursionError, ValueError, TypeError):
        return None


class _Oracle:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.root = Path(os.environ["AXIOM_PIN_ORACLE_ROOT"]).resolve()
        self.out = Path(os.environ["AXIOM_PIN_ORACLE_OUT"])
        self.wanted = {(path, name) for path, name in json.loads(os.environ["AXIOM_PIN_ORACLE_PINS"])}
        self.index: dict[int, int] = {}  # id(item) -> position in the collection
        self.current: int | None = None
        self.record: dict = {
            "pytest": pytest.__version__,
            "rootdir": self._relative(config.rootpath),
            "inifile": self._relative(config.inipath) if config.inipath else None,
            "addopts": list(config.getini("addopts")),
            "xdist": bool(getattr(config.option, "numprocesses", None))
            or getattr(config.option, "dist", "no") not in ("no", None),
            "collected": [],
            "deselected": [],
            "collect_errors": [],
            "collect_skips": [],
            "kept": [],
            "reports": {},
            "exitstatus": None,
        }

    def _relative(self, path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def key(self, item: pytest.Item) -> tuple[str, str]:
        """(path, name) as the register spells it: `function` or `Class::function`,
        or a YAML case's `name` (policyengine-core's YamlItem carries its case as
        `item.test` and has no name until it runs)."""

        case = getattr(item, "test", None)
        if isinstance(case, dict) and not isinstance(item, pytest.Function):
            name = case.get("name")
            return self._relative(item.path), name if isinstance(name, str) else ""
        names = []
        seen_module = False
        for node in item.listchain():
            if isinstance(node, pytest.Module):
                seen_module = True
                continue
            if not seen_module or node is item or node.name == "()":
                continue
            names.append(node.name)
        names.append(getattr(item, "originalname", None) or item.name)
        return self._relative(item.path), "::".join(names)

    @pytest.hookimpl
    def pytest_itemcollected(self, item: pytest.Item) -> None:
        path, name = self.key(item)
        self.index[id(item)] = len(self.record["collected"])
        collected = {"index": self.index[id(item)], "nodeid": item.nodeid, "path": path, "name": name}
        case = getattr(item, "test", None)
        if isinstance(case, dict) and not isinstance(item, pytest.Function) and (path, name) in self.wanted:
            collected["case"] = {
                "input_keys": _input_keys(case.get("input")),
                "output": _jsonable(case.get("output")),
                # What else decides whether the case shows the model choosing:
                # a reform or extension, and the error margins it allows.
                "reforms": bool(case.get("reforms")),
                "extensions": bool(case.get("extensions")),
                "absolute_error_margin": _jsonable(case.get("absolute_error_margin")),
                "relative_error_margin": _jsonable(case.get("relative_error_margin")),
            }
        self.record["collected"].append(collected)

    @pytest.hookimpl
    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed or report.skipped:
            # A node id's path part is relative to the rootdir, which need not
            # be the checkout root.
            path = report.nodeid.split("::")[0]
            self.record["collect_errors" if report.failed else "collect_skips"].append(
                {
                    "nodeid": report.nodeid,
                    "path": self._relative(self.config.rootpath / path) if path else None,
                    "message": str(report.longrepr)[-_MESSAGE_LIMIT:],
                }
            )

    @pytest.hookimpl
    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        # An item the oracle never saw collected has no index; it can't be a
        # pinned case, and its node id is kept for the log.
        self.record["deselected"].extend(self.index.get(id(item), item.nodeid) for item in items)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        # The repository's own -k/-m selection and conftest hooks have run;
        # what remains and is pinned runs, and nothing else does.
        items[:] = [item for item in items if self.key(item) in self.wanted]
        self.record["kept"] = [self.index.get(id(item), item.nodeid) for item in items]

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item):
        self.current = self.index.get(id(item))
        try:
            yield
        finally:
            self.current = None

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # Keyed by the running item's index; a report outside any protocol
        # (it should not happen) is kept under its node id, where no case
        # can claim it.
        key = str(self.current) if self.current is not None else f"nodeid:{report.nodeid}"
        phases = self.record["reports"].setdefault(key, {})
        if report.when in phases:
            # Two reports for one node id and phase (a duplicated or renamed
            # item): a later pass must not hide an earlier failure, so the
            # phase becomes an outcome the runner never passes.
            phases[report.when] = {"outcome": "duplicate", "xfail": False, "message": "reported more than once"}
            return
        phases[report.when] = {
            "outcome": report.outcome,
            "xfail": hasattr(report, "wasxfail"),
            "message": (report.longreprtext or "")[-_MESSAGE_LIMIT:],
        }

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.record["exitstatus"] = int(exitstatus)
        self.out.write_text(json.dumps(self.record, indent=1))


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        return  # an xdist worker; the runner refuses distributed runs
    config.pluginmanager.register(_Oracle(config), "axiom-pin-oracle")
