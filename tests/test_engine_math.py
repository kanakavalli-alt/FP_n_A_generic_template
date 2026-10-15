"""Primitives + formulas: exact netting on random inputs, and the edge cases
the Kaplan build hit in production."""
import random
from decimal import Decimal as D

import pytest

from fpa_agent.engine import primitives as P
from fpa_agent.engine.formulas import FormulaError, evaluate, references
from fpa_agent.engine.results import Driver, NettingError, assert_nets, round_to_total

rnd = random.Random(7)


def rand(lo=1, hi=10_000, dp=2):
    return D(str(round(rnd.uniform(lo, hi), dp)))


@pytest.mark.parametrize("_", range(50))
def test_price_volume_nets(_):
    r = P.price_volume(rand(), rand(), rand(), rand())
    assert r.nets_to_headline and sum(d.contribution for d in r.drivers) == r.headline


@pytest.mark.parametrize("_", range(50))
def test_price_volume_mix_nets_with_zero_volume_and_exclusions(_):
    cats = ["A", "B", "C", "D", "ZERO", "OTHER"]
    prior = {c: (D(0) if c == "ZERO" else rand(10, 500, 0), rand(1, 300, 0)) for c in cats}
    curr = {c: (D(0) if c == "ZERO" else rand(10, 500, 0), rand(1, 300, 0)) for c in cats[1:]}  # A disappears
    r = P.price_volume_mix(prior, curr, excluded_categories=["other"])
    assert abs(sum(d.contribution for d in r.drivers) - r.headline) < D("1e-20")
    unattributed = next(d for d in r.drivers if d.role == "unattributed").contribution
    expected = (curr["ZERO"][1] + curr["OTHER"][1]) - (prior["ZERO"][1] + prior["OTHER"][1])
    assert unattributed == expected  # zero-volume + excluded value never vanishes


def test_simpsons_paradox_visible():
    # every channel's rate falls, but mix shifts to the high-rate channel
    prior = {"low": (D(900), D(90)), "high": (D(100), D(50))}
    curr = {"low": (D(500), D(45)), "high": (D(500), D(240))}
    r = P.price_volume_mix(prior, curr)
    d = {x.role: x.contribution for x in r.drivers}
    assert d["rate"] < 0 < d["mix"] and r.headline > 0


@pytest.mark.parametrize("_", range(30))
def test_ratio_bridge_nets(_):
    r = P.ratio_bridge(rand(), rand(), rand(), rand())
    assert abs(sum(d.contribution for d in r.drivers) - r.headline) < D("1e-20")


def test_contribution_names_non_additive_overlap():
    r = P.contribution({"x": D(60), "y": D(50)}, {"x": D(40), "y": D(40)}, D(100), D(75))
    roles = {d.role: d.contribution for d in r.drivers}
    assert roles["non_additive_overlap"] == D(25) - D(30) and r.nets_to_headline


def test_rollforward_other_flows_is_load_bearing():
    r = P.rollforward_bridge(D(100), D(90), {"IN": D(20), "OUT": D(-5)}, {"IN": D(15), "OUT": D(-3)},
                             cy_end=D(118), py_end=D(102))
    d = {x.label: x.contribution for x in r.drivers}
    # computed endings 115 / 102 -> other_flows = (118-115) - (102-102) = 3
    assert d["other_flows"] == D(3) and r.headline == D(16) and r.nets_to_headline


def test_assert_nets_raises():
    with pytest.raises(NettingError):
        assert_nets([Driver(label="a", contribution=D(1))], D(2))


@pytest.mark.parametrize("_", range(30))
def test_round_to_total_sums_exactly(_):
    vals = {f"d{i}": rand(-500, 500, 4) for i in range(6)}
    total = sum(vals.values())
    for dp in (0, 2):
        shown = round_to_total(vals, total, dp)
        assert sum(shown.values()) == total.quantize(D(1).scaleb(-dp))


def test_formula_evaluation_and_refs():
    assert references("{a} / ({b} + {a})") == ["a", "b"]
    assert evaluate("{a} / {b} * 100", {"a": D(1), "b": D(8)}) == D("12.5")
    assert evaluate("abs({a} - {b})", {"a": D(1), "b": D(3)}) == D(2)
    assert evaluate("{a} + {b}", {"a": D(1), "b": None}) is None


@pytest.mark.parametrize("bad", ["__import__('os')", "{a}.real", "[1,2]", "{a} ** 2", "lambda: 1", "open('x')"])
def test_formula_rejects_non_arithmetic(bad):
    with pytest.raises(FormulaError):
        evaluate(bad, {"a": D(1)})


def test_formula_division_by_zero():
    with pytest.raises(FormulaError):
        evaluate("{a} / {b}", {"a": D(1), "b": D(0)})
