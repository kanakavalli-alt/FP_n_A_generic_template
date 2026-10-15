from datetime import date

import pytest

from fpa_agent.periods import FiscalCalendar
from fpa_agent.semantic.models import Calendar

JULY_FY = Calendar(fiscal_year_start_month=7, fiscal_year_label="end", close_lag_days=5)
CAL_FY = Calendar()


def cal(cfg=JULY_FY, today=date(2026, 10, 15)):
    return FiscalCalendar(cfg, today)


def test_fiscal_year_bounds_end_label():
    p = cal().resolve({"type": "year", "fiscal": True, "year": 2026})
    assert (p.start, p.end, p.label) == (date(2025, 7, 1), date(2026, 7, 1), "FY2026")


def test_fiscal_year_start_label():
    c = cal(Calendar(fiscal_year_start_month=4, fiscal_year_label="start"))
    p = c.resolve({"type": "year", "fiscal": True, "year": 2026})
    assert (p.start, p.end) == (date(2026, 4, 1), date(2027, 4, 1))


def test_fiscal_quarter():
    p = cal().resolve({"type": "quarter", "fiscal": True, "year": 2026, "quarter": 3})
    assert (p.start, p.end, p.label) == (date(2026, 1, 1), date(2026, 4, 1), "FY2026 Q3")


def test_calendar_quarter_when_fiscal_false():
    p = cal().resolve({"type": "quarter", "fiscal": False, "year": 2026, "quarter": 3})
    assert (p.start, p.end) == (date(2026, 7, 1), date(2026, 10, 1))


def test_last_closed_month_respects_close_lag():
    assert cal(today=date(2026, 10, 3)).last_closed_month() == (2026, 8)   # Sep not closed until Oct 6
    assert cal(today=date(2026, 10, 15)).last_closed_month() == (2026, 9)


def test_january_wraparound():
    assert cal(CAL_FY, date(2026, 1, 20)).last_closed_month() == (2025, 12)
    p = cal(CAL_FY, date(2026, 1, 20)).resolve({"type": "month", "offset": -1})
    assert (p.start, p.end) == (date(2025, 12, 1), date(2026, 1, 1))


def test_ytd_defaults_to_last_closed_month_fiscal():
    p = cal().resolve({"type": "ytd"})
    assert (p.start, p.end) == (date(2026, 7, 1), date(2026, 10, 1))
    assert "FY2027 YTD" in p.label


def test_fiscal_ytd_for_named_year_caps_at_last_closed():
    p = cal(today=date(2026, 3, 20)).resolve({"type": "ytd", "year": 2026})
    assert (p.start, p.end) == (date(2025, 7, 1), date(2026, 3, 1))


def test_range_across_years():
    p = cal().resolve({"type": "range", "start_year": 2025, "start_month": 11, "end_year": 2026, "end_month": 2})
    assert (p.start, p.end, len(p.months)) == (date(2025, 11, 1), date(2026, 3, 1), 4)


def test_comparisons():
    c = cal()
    q = c.resolve({"type": "quarter", "fiscal": True, "year": 2026, "quarter": 1})
    py = c.comparison(q, "prior_year")
    pp = c.comparison(q, "prior_period")
    assert (py.start, py.end, py.label) == (date(2024, 7, 1), date(2024, 10, 1), "FY2025 Q1")
    assert (pp.start, pp.end, pp.label) == (date(2025, 4, 1), date(2025, 7, 1), "FY2025 Q4")


def test_leap_day_prior_year():
    c = cal(CAL_FY)
    p = c.resolve({"type": "range", "start_date": "2024-02-29", "end_date": "2024-02-29"})
    assert c.comparison(p, "prior_year").start == date(2023, 2, 28)


def test_last_n_months():
    p = cal().resolve({"type": "last_n_months", "n": 3})
    assert (p.start, p.end) == (date(2026, 7, 1), date(2026, 10, 1))


@pytest.mark.parametrize("spec", [{"type": "month", "year": 2026, "month": 13},
                                  {"type": "quarter", "year": 2026, "quarter": 5, "fiscal": False},
                                  {"type": "nonsense"}])
def test_invalid_specs(spec):
    with pytest.raises(ValueError):
        cal().resolve(spec)
