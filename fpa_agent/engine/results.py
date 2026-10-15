"""Typed results + the netting guarantee (ported from the Kaplan variance
engine, unchanged in spirit): every decomposition's signed drivers must sum to
its headline within a Decimal epsilon, or a NettingError is raised. A residual
is never rounded over or narrated."""
from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, getcontext
from typing import Any, Sequence

from pydantic import BaseModel, Field

getcontext().prec = 34

EPSILON_MONEY = Decimal("0.01")
EPSILON_RATE = Decimal("0.0001")


def D(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        raise ValueError("cannot convert None to Decimal")
    return Decimal(str(x))


class Driver(BaseModel):
    label: str
    contribution: Decimal
    role: str = ""          # machine role (volume/price/mix/...) — label is display


class VarianceResult(BaseModel):
    method: str
    headline: Decimal                     # the change being explained
    drivers: list[Driver]
    nets_to_headline: bool
    current_value: Decimal | None = None
    comparison_value: Decimal | None = None
    presentation: dict[str, Any] = Field(default_factory=dict)
    post_netting: list[Driver] = Field(default_factory=list)   # e.g. timing adjustments


class NettingError(Exception):
    """Drivers do not net to the headline. An error, never a number to narrate."""


def assert_nets(drivers: Sequence[Driver], headline: Decimal, eps: Decimal = EPSILON_RATE) -> bool:
    total = sum((d.contribution for d in drivers), Decimal("0"))
    residual = abs(total - headline)
    if residual > eps:
        raise NettingError(
            f"drivers do not net to headline: sum={total} headline={headline} "
            f"residual={residual} > eps={eps}"
        )
    return True


def quantize(v: Decimal | None, decimals: int) -> str | None:
    if v is None:
        return None
    q = Decimal(1).scaleb(-decimals)
    return str(v.quantize(q, rounding=ROUND_HALF_UP))


def pct(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / abs(denominator) * 100


def round_to_total(values: dict[str, Decimal], target: Decimal, decimals: int = 0) -> dict[str, Decimal]:
    """Round driver contributions for DISPLAY so the rounded drivers still sum
    exactly to the rounded headline (largest-remainder allocation). The real
    netting proof already ran on unrounded Decimals."""
    scale = Decimal(10) ** decimals
    scaled = {k: v * scale for k, v in values.items()}
    target_units = int((target * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    floors = {k: int(v.to_integral_value(rounding=ROUND_FLOOR)) for k, v in scaled.items()}
    result = dict(floors)
    remainder = target_units - sum(floors.values())
    order = sorted(scaled, key=lambda k: scaled[k] - floors[k], reverse=True)
    i = 0
    while remainder > 0 and order:
        result[order[i % len(order)]] += 1; remainder -= 1; i += 1
    i = 0
    while remainder < 0 and order:
        result[order[-1 - (i % len(order))]] -= 1; remainder += 1; i += 1
    return {k: Decimal(v) / scale for k, v in result.items()}


def favorability(change: Decimal | None, direction: str) -> str | None:
    if change is None or direction == "neutral" or change == 0:
        return None
    good = change > 0 if direction == "higher" else change < 0
    return "favorable" if good else "unfavorable"
