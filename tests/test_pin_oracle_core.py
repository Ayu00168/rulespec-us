"""The optimum check against policyengine-core's own YAML runner.

tests/test_pin_runner.py drives the oracle with a stand-in for core's YAML
items. This file runs the real thing where policyengine-core is installed (it
is not in this repository's CI, so there it skips): a minimal model, cases
that force, imitate or override a choice, core's `OpenFiscaPlugin`, the
oracle, and `optimum_shown` on what the oracle recorded. Each case is one the
#1844 reviews executed against core, or the legitimate case next to it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("policyengine_core")

TOOLS = Path(__file__).resolve().parents[1] / "tools"

MODEL = textwrap.dedent(
    """\
    from policyengine_core.entities import build_entity
    from policyengine_core.enums import Enum
    from policyengine_core.model_api import where
    from policyengine_core.parameters import ParameterNode
    from policyengine_core.periods import YEAR
    from policyengine_core.taxbenefitsystems import TaxBenefitSystem
    from policyengine_core.variables import Variable

    Person = build_entity(key="person", plural="people", label="Person", is_person=True)
    Household = build_entity(key="household", plural="households", label="Household",
                             roles=[{"key": "member", "plural": "members", "label": "Member"}])


    class FilingStatus(Enum):
        JOINT = "Joint"
        SEPARATE = "Separate"


    class income(Variable):
        value_type = float
        entity = Person
        definition_period = YEAR
        label = "income"


    class elects(Variable):
        # The choice: elected above an income of 10, while gov.flag is on.
        value_type = bool
        entity = Person
        definition_period = YEAR
        label = "elects"

        def formula(person, period, parameters):
            return (person("income", period) > 10) & parameters(period).gov.flag


    class checked(Variable):
        value_type = bool
        entity = Person
        definition_period = YEAR
        label = "checked"

        def formula(person, period, parameters):
            return person("income", period) > 5


    class p0(Variable):
        # A choice named like the instance id an axis generates.
        value_type = bool
        entity = Person
        definition_period = YEAR
        label = "p0"


    class share(Variable):
        value_type = float
        entity = Person
        definition_period = YEAR
        label = "share"

        def formula(person, period, parameters):
            return where(person("income", period) > 10, 0.3, 0.5)


    class filing_status(Variable):
        value_type = Enum
        possible_values = FilingStatus
        default_value = FilingStatus.JOINT
        entity = Person
        definition_period = YEAR
        label = "filing status"

        def formula(person, period, parameters):
            return where(person("income", period) > 10, FilingStatus.SEPARATE, FilingStatus.JOINT)


    class MiniSystem(TaxBenefitSystem):
        def __init__(self):
            super().__init__([Person, Household])
            for variable in (income, elects, checked, p0, share, filing_status):
                self.add_variable(variable)
            self.parameters = ParameterNode("", data={"gov": {"flag": {"values": {"2000-01-01": True}}}})
    """
)

# name -> (case, (variable, chooses, default), shown)
CASES = {
    "the model chooses": (
        {"input": {"income": 20}, "output": {"elects": True}},
        ("elects", True, False), True),
    "forced by input": (
        {"input": {"income": 0, "elects": True}, "output": {"elects": True}},
        ("elects", True, False), False),
    "forced through axes": (
        {"input": {"people": {"p": {"income": 0}}, "axes": [[{"name": "elects", "count": 1, "min": 1, "max": 1}]]},
         "output": {"elects": True}},
        ("elects", True, False), False),
    # Core's YAML runner compares within 1e-3 when a case gives no margin.
    "an implicit margin admits the default": (
        {"input": {"income": 0}, "output": {"share": 0.5005}},
        ("share", "0.5005", "0.5"), False),
    # A margin of 0.99999999 rounds, in float32, to 1: false passes as true.
    "a boolean margin admits the default": (
        {"input": {"income": 0}, "output": {"elects": True}, "absolute_error_margin": 0.99999999},
        ("elects", True, False), False),
    "a float32 share the model chose": (
        {"input": {"income": 20}, "output": {"share": 0.3}},
        ("share", "0.3", "0.5"), True),
    # 0.500000001 is the default's float32 number, so the case can't tell them apart.
    "an option the default's float32 number": (
        {"input": {"income": 0}, "output": {"share": 0.500000001}, "absolute_error_margin": 0},
        ("share", "0.500000001", "0.5"), False),
    # The axis names the instance p0; the output asserts `checked` for it, not the choice p0.
    "an instance named like the choice": (
        {"input": {"people": {"p": {"income": 6}}, "axes": [[{"name": "checked", "count": 1, "min": 1, "max": 1}]]},
         "output": {"people": {"p0": {"checked": True}}}},
        ("p0", True, False), False),
    "a dotted entity id is not a parameter": (
        {"input": {"people": {"person.a": {"income": 20}}}, "output": {"elects": True}},
        ("elects", True, False), True),
    "a parameter override": (
        {"input": {"gov.flag": True, "income": 20}, "output": {"elects": True}},
        ("elects", True, False), False),
    "an enum the model chose": (
        {"input": {"income": 20}, "output": {"filing_status": "SEPARATE"}},
        ("filing_status", "separate", "joint"), True),
    "an enum the model did not choose": (
        {"input": {"income": 0}, "output": {"filing_status": "JOINT"}},
        ("filing_status", "separate", "joint"), False),
}

DRIVER = textwrap.dedent(
    """\
    import json, sys
    import pytest
    from policyengine_core.tools.test_runner import OpenFiscaPlugin
    from minimodel import MiniSystem

    code = pytest.main(["-q", "-p", "axiom_pin_oracle", "-p", "no:cacheprovider", "cases.yaml"],
                       plugins=[OpenFiscaPlugin(MiniSystem(), {})])
    sys.exit(int(code))
    """
)


def _load_runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pin_runner_core", TOOLS / "pin_runner.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_oracle_reads_what_core_computed(tmp_path):
    runner = _load_runner()
    cases = [{"name": name, "period": 2026, **case} for name, (case, _, _) in CASES.items()]
    (tmp_path / "minimodel.py").write_text(MODEL)
    (tmp_path / "driver.py").write_text(DRIVER)
    (tmp_path / "cases.yaml").write_text(json.dumps(cases))  # JSON is YAML
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(TOOLS / "pin_oracle")]),
        "AXIOM_PIN_ORACLE_OUT": str(tmp_path / "oracle.json"),
        "AXIOM_PIN_ORACLE_ROOT": str(tmp_path),
        "AXIOM_PIN_ORACLE_PINS": json.dumps([["cases.yaml", name] for name in CASES]),
        "AXIOM_PIN_ORACLE_CHOICES": json.dumps([["cases.yaml", name, expect[0]] for name, (_, expect, _) in CASES.items()]),
    }
    process = subprocess.run([sys.executable, "driver.py"], cwd=tmp_path, env=env, capture_output=True, text=True,
                             timeout=600)
    assert process.returncode == 0, process.stdout[-3000:] + process.stderr[-3000:]
    record = json.loads((tmp_path / "oracle.json").read_text())
    verdicts = {}
    for item in record["collected"]:
        phases = record["reports"].get(str(item["index"]), {})
        assert [phases[when]["outcome"] for when in ("setup", "call", "teardown")] == ["passed"] * 3, item["name"]
        _, expect, _ = CASES[item["name"]]
        verdicts[item["name"]] = runner.optimum_shown(item, expect)
    assert set(verdicts) == set(CASES)
    # Every case passes as a test; only those the model chose show an optimum.
    assert {name: reason is None for name, reason in verdicts.items()} == {
        name: shown for name, (_, _, shown) in CASES.items()
    }, verdicts
    assert "sets elects as an input" in verdicts["forced through axes"]
    assert "computed share = 0.5" in verdicts["an implicit margin admits the default"]
    assert "computed elects = False" in verdicts["a boolean margin admits the default"]
    assert "is the default" in verdicts["an option the default's float32 number"]
    assert "computed p0 = False" in verdicts["an instance named like the choice"]
    assert "changes the model (gov.flag)" in verdicts["a parameter override"]
    assert "computed filing_status = 'JOINT'" in verdicts["an enum the model did not choose"]
