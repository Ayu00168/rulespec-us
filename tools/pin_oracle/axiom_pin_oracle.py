"""pytest plugin that tools/pin_runner.py loads into a pinned repository's run.

It records what pytest itself did, under the repository's own ini, conftest
files and addopts: every collected item, every item the configuration
deselected, collection errors and module-level skips, and each phase report
of the pinned items. tools/pin_runner.py classifies the record; nothing here
decides whether a pin passed. Items the register does not pin are dropped
after the repository's own selection has run, so only pinned tests execute.

Environment (set by the runner):
- AXIOM_PIN_ORACLE_OUT: where to write the JSON record.
- AXIOM_PIN_ORACLE_ROOT: the checkout root; paths are recorded relative to it.
- AXIOM_PIN_ORACLE_PINS: JSON list of [path, name] pairs, name being
  `function` or `Class::function`.

It imports nothing but pytest and the standard library.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_MESSAGE_LIMIT = 2000


class _Oracle:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.root = Path(os.environ["AXIOM_PIN_ORACLE_ROOT"]).resolve()
        self.out = Path(os.environ["AXIOM_PIN_ORACLE_OUT"])
        self.wanted = {(path, name) for path, name in json.loads(os.environ["AXIOM_PIN_ORACLE_PINS"])}
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
        """(path, name) as the register spells it: `function` or `Class::function`."""

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
        self.record["collected"].append({"nodeid": item.nodeid, "path": path, "name": name})

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
        self.record["deselected"].extend(item.nodeid for item in items)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        # The repository's own -k/-m selection and conftest hooks have run;
        # what remains and is pinned runs, and nothing else does.
        items[:] = [item for item in items if self.key(item) in self.wanted]
        self.record["kept"] = [item.nodeid for item in items]

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        phases = self.record["reports"].setdefault(report.nodeid, {})
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
