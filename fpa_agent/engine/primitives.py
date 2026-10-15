"""
Pure, netting-guaranteed decomposition primitives. No I/O, no semantic-layer
knowledge, no client vocabulary: every label comes from the analysis row's
`params.labels` (falling back to neutral role names).

Each primitive is an exact algebraic identity, verified by assert_nets:

  delta              ΔY = Y1 − Y0
  contribution       ΔY = Σ_c ΔY_c  (+ non_additive_overlap when the metric is
                     not additive across the dimension, e.g. COUNT DISTINCT)
  price_volume       ΔR = p0·ΔV + ΔP·V0 + ΔP·ΔV
  mix_rate           ΔY = Σ Δs·r0 + Σ s0·Δr + Σ Δs·Δr
  price_volume_mix   ΔR = volume + mix + rate + interactions + unattributed
                     (Kaplan UC1/UC4 shape, generalised; zero-volume and
                     excluded categories go to an explicit `unattributed`
                     driver instead of silently falling out of the proof)
  ratio_bridge       Δ(N/D) = ΔN/D1 + (N0/D1 − N0/D0)
  rollforward_bridge ΔEnd = ΔCarryIn + Σ ΔFlow_c + Δother   (Kaplan UC2 shape)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from fpa_agent.engine.results import EPSILON_MONEY, EPSILON_RATE, Driver, VarianceResult, assert_nets

ZERO = Decimal("0")


def _lbl(labels: Mapping[str, str] | None, role: str) -> str:
    return (labels or {}).get(role, role)


def delta(current: Decimal, comparison: Decimal, labels=None) -> VarianceResult:
    drivers = [Driver(label=_lbl(labels, "change"), role="change", contribution=current - comparison)]
    headline = current - comparison
    return VarianceResult(method="delta", headline=headline, drivers=drivers,
                          nets_to_headline=assert_nets(drivers, headline),
                          current_value=current, comparison_value=comparison)


def contribution(current_by: Mapping[str, Decimal | None], comparison_by: Mapping[str, Decimal | None],
                 current_total: Decimal, comparison_total: Decimal, labels=None) -> VarianceResult:
    cats = list(dict.fromkeys([*comparison_by, *current_by]))
    drivers: list[Driver] = []
    for c in cats:
        d = (current_by.get(c) or ZERO) - (comparison_by.get(c) or ZERO)
        drivers.append(Driver(label=str(c), role="category", contribution=d))
    headline = current_total - comparison_total
    gap = headline - sum((d.contribution for d in drivers), ZERO)
    # Sub-cent gaps are float noise from the source's own SUM, not overlap.
    if abs(gap) >= Decimal("0.005"):
        # The metric is not additive across this dimension (distinct counts,
        # entities in several categories, rows with a NULL category...). Name
        # the gap rather than hide it — Kaplan's "count overlap" lesson.
        drivers.append(Driver(label=_lbl(labels, "non_additive_overlap"),
                              role="non_additive_overlap", contribution=gap))
    drivers.sort(key=lambda d: (d.role != "category", -abs(d.contribution)))
    return VarianceResult(method="contribution", headline=headline, drivers=drivers,
                          nets_to_headline=assert_nets(drivers, headline, EPSILON_MONEY),
                          current_value=current_total, comparison_value=comparison_total)


def price_volume(p0: Decimal, v0: Decimal, p1: Decimal, v1: Decimal, labels=None) -> VarianceResult:
    dp, dv = p1 - p0, v1 - v0
    drivers = [
        Driver(label=_lbl(labels, "volume"), role="volume", contribution=p0 * dv),
        Driver(label=_lbl(labels, "price"), role="price", contribution=dp * v0),
        Driver(label=_lbl(labels, "interaction"), role="interaction", contribution=dp * dv),
    ]
    headline = p1 * v1 - p0 * v0
    return VarianceResult(method="price_volume", headline=headline, drivers=drivers,
                          nets_to_headline=assert_nets(drivers, headline),
                          current_value=p1 * v1, comparison_value=p0 * v0)


def mix_rate(prior: Mapping[str, tuple[Decimal, Decimal]],
             current: Mapping[str, tuple[Decimal, Decimal]], labels=None) -> VarianceResult:
    """prior/current: category -> (share, rate)."""
    mix = rate = inter = headline = ZERO
    for c in dict.fromkeys([*prior, *current]):
        s0, r0 = prior.get(c, (ZERO, ZERO))
        s1, r1 = current.get(c, (ZERO, ZERO))
        ds, dr = s1 - s0, r1 - r0
        mix += ds * r0
        rate += s0 * dr
        inter += ds * dr
        headline += s1 * r1 - s0 * r0
    drivers = [
        Driver(label=_lbl(labels, "mix"), role="mix", contribution=mix),
        Driver(label=_lbl(labels, "rate"), role="rate", contribution=rate),
        Driver(label=_lbl(labels, "mix_interaction"), role="mix_interaction", contribution=inter),
    ]
    return VarianceResult(method="mix_rate", headline=headline, drivers=drivers,
                          nets_to_headline=assert_nets(drivers, headline))


def price_volume_mix(prior: Mapping[str, tuple[Decimal, Decimal]],
                     current: Mapping[str, tuple[Decimal, Decimal]],
                     labels=None, excluded_categories=(), rate_is_ratio: bool = True) -> VarianceResult:
    """prior/current: category -> (volume, value). Headline = Δ total value.

    Generalisation of Kaplan's pvm∘mix_rate dispatcher:
      volume driver   = b0 · ΔV               (blended rate b = value/volume)
      mix / rate      = mix_rate over driven categories, scaled by V0
      interactions    = mix and pvm interaction terms
      unattributed    = value movement in zero-volume or excluded categories
    """
    excluded = {str(c).strip().upper() for c in excluded_categories}

    def driven(rows):
        return {c: (v, val) for c, (v, val) in rows.items()
                if v not in (None, ZERO) and str(c).strip().upper() not in excluded}

    p_drv, c_drv = driven(prior), driven(current)
    V0 = sum((v for v, _ in p_drv.values()), ZERO)
    V1 = sum((v for v, _ in c_drv.values()), ZERO)
    if V0 == 0 or V1 == 0:
        raise ValueError("no categories with volume remain after exclusions; "
                         "price_volume_mix cannot compute a rate driver")
    S0d = sum((val or ZERO for _, val in p_drv.values()), ZERO)
    S1d = sum((val or ZERO for _, val in c_drv.values()), ZERO)
    b0, b1 = S0d / V0, S1d / V1

    pv = price_volume(b0, V0, b1, V1)
    pvd = {d.role: d.contribution for d in pv.drivers}
    mr = mix_rate({c: (v / V0, (val or ZERO) / v) for c, (v, val) in p_drv.items()},
                  {c: (v / V1, (val or ZERO) / v) for c, (v, val) in c_drv.items()})
    mrd = {d.role: d.contribution for d in mr.drivers}

    S0 = sum((val or ZERO for _, val in prior.values()), ZERO)
    S1 = sum((val or ZERO for _, val in current.values()), ZERO)
    unattributed = (S1 - S1d) - (S0 - S0d)

    drivers = [
        Driver(label=_lbl(labels, "volume"), role="volume", contribution=pvd["volume"]),
        Driver(label=_lbl(labels, "mix"), role="mix", contribution=mrd["mix"] * V0),
        Driver(label=_lbl(labels, "rate"), role="rate", contribution=mrd["rate"] * V0),
        Driver(label=_lbl(labels, "mix_interaction"), role="mix_interaction",
               contribution=mrd["mix_interaction"] * V0),
        Driver(label=_lbl(labels, "volume_interaction"), role="volume_interaction",
               contribution=pvd["interaction"]),
        Driver(label=_lbl(labels, "unattributed"), role="unattributed", contribution=unattributed),
    ]
    headline = S1 - S0
    rate_move = (b1 - b0) * 100 if rate_is_ratio else ((b1 - b0) / b0 * 100 if b0 else ZERO)
    return VarianceResult(
        method="price_volume_mix", headline=headline, drivers=drivers,
        nets_to_headline=assert_nets(drivers, headline),
        current_value=S1, comparison_value=S0,
        presentation={
            "blended_rate_current": b1, "blended_rate_comparison": b0,
            ("rate_change_pp" if rate_is_ratio else "rate_change_pct"): rate_move,
            "volume_current": V1, "volume_comparison": V0,
        },
    )


def ratio_bridge(n0: Decimal, d0: Decimal, n1: Decimal, d1: Decimal, labels=None) -> VarianceResult:
    if d0 == 0 or d1 == 0:
        raise ValueError("ratio_bridge: denominator is zero")
    r0, r1 = n0 / d0, n1 / d1
    drivers = [
        Driver(label=_lbl(labels, "numerator_effect"), role="numerator_effect", contribution=(n1 - n0) / d1),
        Driver(label=_lbl(labels, "denominator_effect"), role="denominator_effect",
               contribution=n0 / d1 - n0 / d0),
    ]
    headline = r1 - r0
    return VarianceResult(method="ratio_bridge", headline=headline, drivers=drivers,
                          nets_to_headline=assert_nets(drivers, headline, EPSILON_RATE),
                          current_value=r1, comparison_value=r0,
                          presentation={"numerator_current": n1, "numerator_comparison": n0,
                                        "denominator_current": d1, "denominator_comparison": d0})


def rollforward_bridge(cy_carry_in: Decimal, py_carry_in: Decimal,
                       cy_flows: Mapping[str, Decimal], py_flows: Mapping[str, Decimal],
                       cy_end: Decimal, py_end: Decimal, labels=None) -> VarianceResult:
    """The headline is the INDEPENDENTLY fetched ending level, so the proof is
    load-bearing (the flows are checked against reality, not themselves); any
    gap is reported as an explicit `other_flows` driver."""
    codes = list(dict.fromkeys([*cy_flows, *py_flows]))
    drivers = [Driver(label=_lbl(labels, "carry_in"), role="carry_in", contribution=cy_carry_in - py_carry_in)]
    for c in codes:
        drivers.append(Driver(label=_lbl(labels, str(c)), role="flow",
                              contribution=(cy_flows.get(c) or ZERO) - (py_flows.get(c) or ZERO)))
    cy_gap = cy_end - (cy_carry_in + sum((v or ZERO for v in cy_flows.values()), ZERO))
    py_gap = py_end - (py_carry_in + sum((v or ZERO for v in py_flows.values()), ZERO))
    drivers.append(Driver(label=_lbl(labels, "other_flows"), role="other_flows", contribution=cy_gap - py_gap))
    headline = cy_end - py_end
    return VarianceResult(
        method="rollforward_bridge", headline=headline, drivers=drivers,
        nets_to_headline=assert_nets(drivers, headline),
        current_value=cy_end, comparison_value=py_end,
        presentation={"carry_in_current": cy_carry_in, "carry_in_comparison": py_carry_in,
                      "reconciliation_gap_current": cy_gap, "reconciliation_gap_comparison": py_gap,
                      **{f"flow_current_{k}": v for k, v in cy_flows.items()},
                      **{f"flow_comparison_{k}": v for k, v in py_flows.items()}},
    )
