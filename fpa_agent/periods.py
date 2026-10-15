"""
Generic, calendar-config-driven period resolution.

The model NEVER does date arithmetic. It describes a period ("Q2 of fiscal
2026", "last month", "YTD", "March through June") as a PeriodSpec and this
module turns it into exact, half-open date bounds using the client's fiscal
calendar from the semantic layer (sl_client_profile.calendar). Kaplan's
hard-coded Jul–Jun fiscal year, January wrap-around and "YTD = through the
last CLOSED month" rules are all special cases of this module.
"""
from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from fpa_agent.semantic.models import Calendar

PeriodType = Literal["month", "quarter", "year", "ytd", "qtd", "range", "last_n_months"]
ComparisonKind = Literal["prior_year", "prior_period", "budget", "none"]


def _add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def _month_start(y: int, m: int) -> date:
    return date(y, m, 1)


def _shift_months(d: date, delta: int) -> date:
    y, m = _add_months(d.year, d.month, delta)
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1]))


def _fmt_month(y: int, m: int) -> str:
    return f"{_cal.month_abbr[m]} {y}"


@dataclass(frozen=True)
class Period:
    start: date              # inclusive
    end: date                # exclusive
    label: str
    grain: str
    fiscal: bool = False

    @property
    def end_inclusive(self) -> date:
        return self.end - timedelta(days=1)

    @property
    def month_aligned(self) -> bool:
        return self.start.day == 1 and self.end.day == 1

    @property
    def months(self) -> list[tuple[int, int]]:
        """Every (year, month) the period touches, in order."""
        out, (y, m) = [], (self.start.year, self.start.month)
        last = (self.end_inclusive.year, self.end_inclusive.month)
        while (y, m) <= last:
            out.append((y, m))
            y, m = _add_months(y, m, 1)
        return out

    @property
    def start_yyyymm(self) -> int:
        return self.start.year * 100 + self.start.month

    @property
    def end_yyyymm(self) -> int:
        e = self.end_inclusive
        return e.year * 100 + e.month

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "grain": self.grain,
            "fiscal": self.fiscal,
            "start_date": self.start.isoformat(),
            "end_date": self.end_inclusive.isoformat(),
            "end_date_exclusive": self.end.isoformat(),
            "months": len(self.months),
            "start_yyyymm": self.start_yyyymm,
            "end_yyyymm": self.end_yyyymm,
        }


class FiscalCalendar:
    def __init__(self, cfg: Calendar | None = None, as_of: date | None = None):
        self.cfg = cfg or Calendar()
        self.today = as_of or datetime.now().date()

    # ── fiscal year helpers ─────────────────────────────────────────────────
    @property
    def fy_start_month(self) -> int:
        return self.cfg.fiscal_year_start_month

    def fiscal_year_of(self, d: date) -> int:
        s = self.fy_start_month
        if s == 1:
            return d.year
        start_year = d.year if d.month >= s else d.year - 1
        return start_year + 1 if self.cfg.fiscal_year_label == "end" else start_year

    def fiscal_year_start(self, fy: int) -> date:
        s = self.fy_start_month
        if s == 1:
            return date(fy, 1, 1)
        start_year = fy - 1 if self.cfg.fiscal_year_label == "end" else fy
        return date(start_year, s, 1)

    def fiscal_quarter_of(self, d: date) -> int:
        fy_start = self.fiscal_year_start(self.fiscal_year_of(d))
        months = (d.year - fy_start.year) * 12 + d.month - fy_start.month
        return months // 3 + 1

    def year_label(self, year: int, fiscal: bool) -> str:
        return f"FY{year}" if fiscal and self.fy_start_month != 1 else str(year)

    # ── closed-month logic ──────────────────────────────────────────────────
    def last_closed_month(self) -> tuple[int, int]:
        """The most recent month that has ended AND passed the close lag."""
        y, m = _add_months(self.today.year, self.today.month, -1)
        last_day = date(y, m, _cal.monthrange(y, m)[1])
        if self.today < last_day + timedelta(days=1 + self.cfg.close_lag_days):
            y, m = _add_months(y, m, -1)
        return y, m

    def is_closed(self, p: Period) -> bool:
        y, m = self.last_closed_month()
        return p.end <= _month_start(*_add_months(y, m, 1))

    # ── resolution ──────────────────────────────────────────────────────────
    def resolve(self, spec: dict[str, Any]) -> Period:
        """Resolve a PeriodSpec dict. Keys:

        type      month | quarter | year | ytd | qtd | range | last_n_months
        fiscal    bool — quarter/year/ytd/qtd are on the fiscal calendar
                  (default: true for year/ytd/quarter when the client's fiscal
                  year doesn't start in January and ytd_basis is fiscal)
        year      explicit year (the FISCAL year label when fiscal=true)
        quarter   1-4
        month     1-12
        offset    relative to the current period (0 = current, -1 = prior);
                  ignored when an explicit year/quarter/month is given
        start_year/start_month/end_year/end_month or start_date/end_date
                  (type=range; end_date inclusive)
        n         number of months (type=last_n_months, ends at last closed)
        """
        spec = {k: v for k, v in (spec or {}).items() if v is not None}
        ptype = spec.get("type", "month")
        offset = int(spec.get("offset", 0))
        default_fiscal = self.fy_start_month != 1
        fiscal = bool(spec.get("fiscal", default_fiscal))

        if ptype == "month":
            if "year" in spec or "month" in spec:
                y, m = int(spec.get("year", self.today.year)), int(spec.get("month", self.today.month))
            else:
                y, m = _add_months(self.today.year, self.today.month, offset)
            _check_month(m)
            return Period(_month_start(y, m), _month_start(*_add_months(y, m, 1)),
                          _fmt_month(y, m), "month")

        if ptype == "last_n_months":
            n = int(spec.get("n", 3))
            if n < 1:
                raise ValueError("n must be >= 1")
            ey, em = self.last_closed_month()
            sy, sm = _add_months(ey, em, -(n - 1))
            return Period(_month_start(sy, sm), _month_start(*_add_months(ey, em, 1)),
                          f"{_fmt_month(sy, sm)} – {_fmt_month(ey, em)}", "range")

        if ptype == "range":
            if "start_date" in spec:
                start = date.fromisoformat(str(spec["start_date"]))
                end = date.fromisoformat(str(spec.get("end_date", spec["start_date"]))) + timedelta(days=1)
                if end <= start:
                    raise ValueError("end_date must be on/after start_date")
                return Period(start, end, f"{start.isoformat()} – {(end - timedelta(days=1)).isoformat()}", "range")
            sy = int(spec.get("start_year", spec.get("year", self.today.year)))
            sm = int(spec["start_month"])
            ey = int(spec.get("end_year", sy))
            em = int(spec.get("end_month", sm))
            _check_month(sm); _check_month(em)
            if (ey, em) < (sy, sm):
                raise ValueError("range end is before range start")
            label = _fmt_month(sy, sm) if (sy, sm) == (ey, em) else f"{_fmt_month(sy, sm)} – {_fmt_month(ey, em)}"
            return Period(_month_start(sy, sm), _month_start(*_add_months(ey, em, 1)), label, "range")

        if ptype == "year":
            if fiscal:
                fy = int(spec["year"]) if "year" in spec else self.fiscal_year_of(self.today) + offset
                start = self.fiscal_year_start(fy)
                return Period(start, _shift_months(start, 12), self.year_label(fy, True), "year", fiscal=True)
            y = int(spec["year"]) if "year" in spec else self.today.year + offset
            return Period(date(y, 1, 1), date(y + 1, 1, 1), str(y), "year")

        if ptype == "quarter":
            if fiscal:
                if "year" in spec or "quarter" in spec:
                    fy = int(spec.get("year", self.fiscal_year_of(self.today)))
                    q = int(spec.get("quarter", self.fiscal_quarter_of(self.today)))
                else:
                    fy, q = self._shift_quarter(self.fiscal_year_of(self.today),
                                                self.fiscal_quarter_of(self.today), offset)
                _check_quarter(q)
                start = _shift_months(self.fiscal_year_start(fy), 3 * (q - 1))
                label = f"{self.year_label(fy, True)} Q{q}" if self.fy_start_month != 1 else f"Q{q} {fy}"
                return Period(start, _shift_months(start, 3), label, "quarter", fiscal=True)
            if "year" in spec or "quarter" in spec:
                y = int(spec.get("year", self.today.year))
                q = int(spec.get("quarter", (self.today.month - 1) // 3 + 1))
            else:
                y, q = self._shift_quarter(self.today.year, (self.today.month - 1) // 3 + 1, offset)
            _check_quarter(q)
            start = date(y, 3 * (q - 1) + 1, 1)
            return Period(start, _shift_months(start, 3), f"Q{q} {y}", "quarter")

        if ptype in ("ytd", "qtd"):
            # Through the explicit month if given, else the last CLOSED month.
            if "year" in spec and "month" in spec:
                ey, em = int(spec["year"]), int(spec["month"])
            elif "month" in spec:
                ey, em = self.today.year, int(spec["month"])
            else:
                ey, em = self.last_closed_month()
            _check_month(em)
            end = _month_start(*_add_months(ey, em, 1))
            anchor = date(ey, em, 1)
            if ptype == "ytd":
                use_fiscal = fiscal and self.cfg.ytd_basis == "fiscal"
                if "year" in spec and "month" not in spec:
                    # "FY2025 YTD" without a month: the whole year if closed,
                    # else through the last closed month.
                    p = self.resolve({"type": "year", "year": spec["year"], "fiscal": use_fiscal})
                    ly, lm = self.last_closed_month()
                    lc_end = _month_start(*_add_months(ly, lm, 1))
                    end = min(p.end, lc_end)
                    if end <= p.start:
                        raise ValueError(f"{p.label} has no closed months yet")
                    em_y, em_m = _add_months(end.year, end.month, -1)
                    return Period(p.start, end, f"{p.label} YTD (through {_fmt_month(em_y, em_m)})",
                                  "ytd", fiscal=use_fiscal)
                if use_fiscal:
                    fy = self.fiscal_year_of(anchor)
                    start = self.fiscal_year_start(fy)
                    label = f"{self.year_label(fy, True)} YTD (through {_fmt_month(ey, em)})"
                else:
                    start = date(ey, 1, 1)
                    label = f"{ey} YTD (through {_fmt_month(ey, em)})"
                return Period(start, end, label, "ytd", fiscal=use_fiscal)
            # qtd
            if fiscal:
                fq = self.fiscal_quarter_of(anchor)
                start = _shift_months(self.fiscal_year_start(self.fiscal_year_of(anchor)), 3 * (fq - 1))
            else:
                start = date(ey, 3 * ((em - 1) // 3) + 1, 1)
            return Period(start, end, f"QTD (through {_fmt_month(ey, em)})", "qtd", fiscal=fiscal)

        raise ValueError(f"unknown period type {ptype!r}")

    @staticmethod
    def _shift_quarter(y: int, q: int, delta: int) -> tuple[int, int]:
        idx = y * 4 + (q - 1) + delta
        return idx // 4, idx % 4 + 1

    # ── comparisons ─────────────────────────────────────────────────────────
    def comparison(self, p: Period, kind: str) -> Period:
        if kind in ("prior_year",):
            start, end = _shift_months(p.start, -12), _shift_months(p.end, -12)
            return Period(start, end, self._relabel(p, start, end, "prior year"), p.grain, p.fiscal)
        if kind == "prior_period":
            if p.month_aligned:
                n = len(p.months)
                start, end = _shift_months(p.start, -n), p.start
            else:
                length = p.end - p.start
                start, end = p.start - length, p.start
            return Period(start, end, self._relabel(p, start, end, "prior period"), p.grain, p.fiscal)
        if kind in ("budget", "none"):
            return p
        raise ValueError(f"unknown comparison {kind!r}")

    def _relabel(self, p: Period, start: date, end: date, fallback: str) -> str:
        """Re-derive a natural label for a shifted period of the same shape."""
        last = end - timedelta(days=1)
        if p.grain == "month":
            return _fmt_month(start.year, start.month)
        if p.grain == "year" and p.fiscal:
            return self.year_label(self.fiscal_year_of(start), True)
        if p.grain == "year":
            return str(start.year)
        if p.grain == "quarter" and p.fiscal:
            fy, fq = self.fiscal_year_of(start), self.fiscal_quarter_of(start)
            return f"{self.year_label(fy, True)} Q{fq}" if self.fy_start_month != 1 else f"Q{fq} {fy}"
        if p.grain == "quarter":
            return f"Q{(start.month - 1) // 3 + 1} {start.year}"
        if p.month_aligned:
            return f"{_fmt_month(start.year, start.month)} – {_fmt_month(last.year, last.month)}"
        return f"{start.isoformat()} – {last.isoformat()} ({fallback})"

    # ── prompt context ──────────────────────────────────────────────────────
    def context(self) -> dict[str, str]:
        """Code-computed temporal facts injected into every agent prompt."""
        ly, lm = self.last_closed_month()
        fy = self.fiscal_year_of(self.today)
        fq = self.fiscal_quarter_of(self.today)
        last_closed_fy = fy - 1
        return {
            "today": self.today.strftime("%B %d, %Y"),
            "current_fiscal_year": self.year_label(fy, True),
            "current_fiscal_quarter": f"Q{fq}",
            "fiscal_year_start_month": _cal.month_name[self.fy_start_month],
            "last_closed_month": _fmt_month(ly, lm),
            "last_closed_fiscal_year": self.year_label(last_closed_fy, True),
            "ytd_basis": self.cfg.ytd_basis,
            "default_comparison": self.cfg.default_comparison,
        }


def _check_month(m: int) -> None:
    if not 1 <= m <= 12:
        raise ValueError(f"month must be 1-12, got {m}")


def _check_quarter(q: int) -> None:
    if not 1 <= q <= 4:
        raise ValueError(f"quarter must be 1-4, got {q}")
