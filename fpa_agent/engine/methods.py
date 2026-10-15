"""
Analysis methods — the generic replacement for Kaplan's per-use-case
fetch planners and dispatchers.

A method declares the ROLES it needs (value, volume, numerator, level, ...),
whether it needs a dimension, and which comparisons it supports. An analysis
row in the semantic layer binds roles to metric ids. The method asks the
context for data by role ("value, current period, grouped by the analysis
dimension") and the context turns that into compiled, executed, evidenced
queries. There is no client vocabulary and no period arithmetic in here.

Adding a method is a one-time, reviewed code change (register below); every
client/analysis with that shape is then catalog-only — same economics as
Kaplan's Tier-2 dispatchers, without the UC naming.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Awaitable, Callable

from fpa_agent.engine import primitives as P
from fpa_agent.engine.results import VarianceResult

if TYPE_CHECKING:  # pragma: no cover
    from fpa_agent.engine.service import AnalysisContext


class MethodError(ValueError):
    pass


@dataclass(frozen=True)
class MethodSpec:
    name: str
    description: str
    required_roles: tuple[str, ...]
    run: Callable[["AnalysisContext"], Awaitable[VarianceResult]]
    needs_dimension: bool = False
    comparisons: tuple[str, ...] = ("prior_year", "prior_period", "budget")
    param_docs: dict[str, str] = field(default_factory=dict)


def _need(value, what: str):
    if value is None:
        raise MethodError(f"no data returned for {what}")
    return value


def _by_label(fetch) -> dict[str, Decimal | None]:
    return {str(k[-1]): v for k, v in fetch.groups.items()}


async def _run_delta(ctx) -> VarianceResult:
    cur, cmp = await ctx.pair("value")
    return P.delta(_need(cur.total, ctx.what("value", "current")),
                   _need(cmp.total, ctx.what("value", "comparison")), ctx.labels)


async def _run_contribution(ctx) -> VarianceResult:
    dim = ctx.require_dimension()
    (cur_t, cmp_t), (cur_g, cmp_g) = await asyncio.gather(
        ctx.pair("value"), ctx.pair("value", group_by=[dim]))
    return P.contribution(_by_label(cur_g), _by_label(cmp_g),
                          _need(cur_t.total, ctx.what("value", "current")),
                          _need(cmp_t.total, ctx.what("value", "comparison")), ctx.labels)


async def _run_price_volume(ctx) -> VarianceResult:
    (v1, v0), (q1, q0) = await asyncio.gather(ctx.pair("value"), ctx.pair("volume"))
    val1, val0 = _need(v1.total, ctx.what("value", "current")), _need(v0.total, ctx.what("value", "comparison"))
    vol1, vol0 = _need(q1.total, ctx.what("volume", "current")), _need(q0.total, ctx.what("volume", "comparison"))
    if vol0 == 0 or vol1 == 0:
        raise MethodError("volume is zero in one period; price cannot be derived")
    return P.price_volume(val0 / vol0, vol0, val1 / vol1, vol1, ctx.labels)


async def _run_price_volume_mix(ctx) -> VarianceResult:
    dim = ctx.require_dimension()
    (v1, v0), (q1, q0) = await asyncio.gather(
        ctx.pair("value", group_by=[dim]), ctx.pair("volume", group_by=[dim]))

    def rows(vol_f, val_f):
        vol, val = _by_label(vol_f), _by_label(val_f)
        return {c: (vol.get(c) or Decimal(0), val.get(c) or Decimal(0))
                for c in dict.fromkeys([*vol, *val])}

    return P.price_volume_mix(rows(q0, v0), rows(q1, v1), ctx.labels,
                              ctx.params.get("excluded_categories", []),
                              bool(ctx.params.get("rate_is_ratio", True)))


async def _run_ratio_bridge(ctx) -> VarianceResult:
    (n1, n0), (d1, d0) = await asyncio.gather(ctx.pair("numerator"), ctx.pair("denominator"))
    return P.ratio_bridge(_need(n0.total, ctx.what("numerator", "comparison")),
                          _need(d0.total, ctx.what("denominator", "comparison")),
                          _need(n1.total, ctx.what("numerator", "current")),
                          _need(d1.total, ctx.what("denominator", "current")), ctx.labels)


async def _run_rollforward(ctx) -> VarianceResult:
    flow_dim = ctx.params.get("flow_dimension")
    if not flow_dim:
        raise MethodError("rollforward_bridge needs params.flow_dimension")
    cur_p, cmp_p = ctx.period, ctx.comparison_period
    cur_carry_p, cmp_carry_p = ctx.month_before(cur_p), ctx.month_before(cmp_p)
    cy_end, py_end, cy_carry, py_carry, cy_flows, py_flows = await asyncio.gather(
        ctx.fetch("level", cur_p), ctx.fetch("level", cmp_p),
        ctx.fetch("level", cur_carry_p), ctx.fetch("level", cmp_carry_p),
        ctx.fetch("flows", cur_p, group_by=[flow_dim]),
        ctx.fetch("flows", cmp_p, group_by=[flow_dim]),
    )
    return P.rollforward_bridge(
        _need(cy_carry.total, f"opening level before {cur_p.label}"),
        _need(py_carry.total, f"opening level before {cmp_p.label}"),
        {k: v or Decimal(0) for k, v in _by_label(cy_flows).items()},
        {k: v or Decimal(0) for k, v in _by_label(py_flows).items()},
        _need(cy_end.total, f"ending level {cur_p.label}"),
        _need(py_end.total, f"ending level {cmp_p.label}"),
        ctx.labels,
    )


METHODS: dict[str, MethodSpec] = {}


def register(spec: MethodSpec) -> None:
    METHODS[spec.name] = spec


for _spec in (
    MethodSpec("delta", "Current vs comparison, single driver.", ("value",), _run_delta),
    MethodSpec("contribution", "Change split by category of one dimension; non-additive overlap named.",
               ("value",), _run_contribution, needs_dimension=True),
    MethodSpec("price_volume", "Value change split into volume, price (value/volume) and interaction.",
               ("value", "volume"), _run_price_volume),
    MethodSpec("price_volume_mix", "Value change split into volume, mix, rate, interactions, unattributed.",
               ("value", "volume"), _run_price_volume_mix, needs_dimension=True,
               param_docs={"excluded_categories": "categories kept out of the rate/mix math",
                           "rate_is_ratio": "true if value/volume is a 0-1 rate (pp), false for a price (%)"}),
    MethodSpec("ratio_bridge", "Change in a ratio split into numerator and denominator effects.",
               ("numerator", "denominator"), _run_ratio_bridge),
    MethodSpec("rollforward_bridge", "Ending-level change split into opening level, signed flows and other.",
               ("level", "flows"), _run_rollforward, comparisons=("prior_year", "prior_period"),
               param_docs={"flow_dimension": "dimension id that categorises flow events"}),
):
    register(_spec)


def get_method(name: str) -> MethodSpec:
    try:
        return METHODS[name]
    except KeyError:
        raise MethodError(f"unknown method {name!r}; registered: {sorted(METHODS)}") from None
