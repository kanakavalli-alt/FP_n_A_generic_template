"""
Build the demo client's two "source systems" as SQLite files:

  demo/data/sis.sqlite  — Student Information System
      enrollment_funnel(yr, mnth, channel, program, is_reportable, leads, starts)
      status_events(event_date, program, event_type, signed_count)
      student_census(snapshot_month 'YYYY-MM', program, active_students)
      fy_enrollment_summary(fiscal_year, program, enrolled_students, tuition_revenue)
  demo/data/erp.sqlite  — Finance / ERP
      revenue_txn(txn_date, program, modality, units, net_amount)
      budget(period_key YYYYMM, modality, budget_net_sales, budget_units)

Deterministic (seeded) so reconciliation tests are reproducible. The shapes
deliberately include the edge cases the Kaplan build hit in production:
zero-volume categories (Re-entry starts with no lead), a census that does not
perfectly roll forward from flows (admin corrections), non-reportable rows that
a metric filter must exclude, and a fiscal year that crosses calendar years.

Usage:  python demo/build_demo_db.py [--out demo/data]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date
from pathlib import Path

CHANNELS = ["Paid Search", "Organic", "Referral", "Aggregator", "Partner"]
PROGRAMS = ["Nursing", "Business", "IT", "Education"]
MODALITIES = ["Online", "Campus", "Hybrid"]
FIRST = (2024, 1)
LAST = (2026, 9)


def months():
    y, m = FIRST
    while (y, m) <= LAST:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def season(m: int) -> float:
    return {1: 1.25, 2: 0.95, 3: 0.9, 4: 0.85, 5: 0.8, 6: 0.9,
            7: 1.1, 8: 1.2, 9: 1.15, 10: 1.0, 11: 0.85, 12: 0.7}[m]


def build(out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    sis_p, erp_p = out / "sis.sqlite", out / "erp.sqlite"
    for p in (sis_p, erp_p):
        if p.exists():
            p.unlink()
    rnd = random.Random(20261015)
    sis, erp = sqlite3.connect(sis_p), sqlite3.connect(erp_p)

    sis.executescript("""
        CREATE TABLE enrollment_funnel (yr INTEGER, mnth INTEGER, channel TEXT, program TEXT,
                                        is_reportable INTEGER, leads INTEGER, starts INTEGER);
        CREATE TABLE status_events (event_date TEXT, program TEXT, event_type TEXT, signed_count INTEGER);
        CREATE TABLE student_census (snapshot_month TEXT, program TEXT, active_students INTEGER);
        CREATE TABLE fy_enrollment_summary (fiscal_year INTEGER, program TEXT,
                                            enrolled_students INTEGER, tuition_revenue REAL);
    """)
    erp.executescript("""
        CREATE TABLE revenue_txn (txn_date TEXT, program TEXT, modality TEXT, units INTEGER, net_amount REAL);
        CREATE TABLE budget (period_key INTEGER, modality TEXT, budget_net_sales REAL, budget_units INTEGER);
    """)

    base_leads = {"Paid Search": 900, "Organic": 650, "Referral": 260, "Aggregator": 520, "Partner": 180}
    base_conv = {"Paid Search": 0.11, "Organic": 0.16, "Referral": 0.24, "Aggregator": 0.07, "Partner": 0.2}
    prog_w = {"Nursing": 0.35, "Business": 0.3, "IT": 0.2, "Education": 0.15}
    census = {p: int(5200 * w) for p, w in prog_w.items()}
    sis.executemany("INSERT INTO student_census VALUES (?,?,?)",
                    [("2023-12", p, v) for p, v in census.items()])

    for y, m in months():
        t = (y - 2024) * 12 + (m - 1)
        starts_by_prog = {p: 0 for p in PROGRAMS}
        for ch in CHANNELS:
            # Paid Search grows its share while every channel's conversion slips
            # slightly: a built-in Simpson's-paradox case for the mix/rate method.
            growth = 1 + (0.012 * t if ch in ("Paid Search", "Aggregator") else 0.002 * t)
            conv = base_conv[ch] * (1 - 0.003 * t)
            for p in PROGRAMS:
                leads = int(base_leads[ch] * prog_w[p] * season(m) * growth * rnd.uniform(0.9, 1.1))
                starts = int(round(leads * conv * rnd.uniform(0.85, 1.15)))
                junk = int(leads * 0.08)  # non-reportable duplicates: must be filtered out
                sis.execute("INSERT INTO enrollment_funnel VALUES (?,?,?,?,?,?,?)", (y, m, ch, p, 1, leads, starts))
                sis.execute("INSERT INTO enrollment_funnel VALUES (?,?,?,?,?,?,?)", (y, m, ch, p, 0, junk, 0))
                starts_by_prog[p] += starts
        for p in PROGRAMS:  # re-entries: starts with no lead (zero-volume category)
            reentry = int(rnd.uniform(4, 14) * prog_w[p] * 4)
            sis.execute("INSERT INTO enrollment_funnel VALUES (?,?,?,?,?,?,?)", (y, m, "Re-entry", p, 1, 0, reentry))
            starts_by_prog[p] += reentry

        for p in PROGRAMS:
            events = {
                "START": starts_by_prog[p],
                "DROP": -int(census[p] * rnd.uniform(0.025, 0.04)),
                "GRAD": -int(census[p] * (0.06 if m in (6, 12) else 0.004) * rnd.uniform(0.8, 1.2)),
                "RETURN": int(census[p] * rnd.uniform(0.003, 0.008)),
            }
            for et, n in events.items():
                # split each month's events over a few dates
                parts = [n // 3, n // 3, n - 2 * (n // 3)]
                for i, part in enumerate(parts):
                    if part:
                        sis.execute("INSERT INTO status_events VALUES (?,?,?,?)",
                                    (date(y, m, 5 + i * 9).isoformat(), p, et, part))
            correction = rnd.choice([0, 0, 0, 1, -1, 2, -3])   # admin corrections -> "other flows"
            census[p] += sum(events.values()) + correction
            sis.execute("INSERT INTO student_census VALUES (?,?,?)", (f"{y:04d}-{m:02d}", p, census[p]))

        for mod in MODALITIES:
            month_units = month_sales = 0
            for p in PROGRAMS:
                price = {"Online": 1180.0, "Campus": 1640.0, "Hybrid": 1395.0}[mod] * (1 + 0.004 * t)
                for day in (3, 14, 25):
                    units = int(starts_by_prog[p] * {"Online": 0.55, "Campus": 0.25, "Hybrid": 0.2}[mod]
                                * rnd.uniform(0.25, 0.42))
                    amount = round(units * price * rnd.uniform(0.94, 1.03), 2)
                    erp.execute("INSERT INTO revenue_txn VALUES (?,?,?,?,?)",
                                (date(y, m, day).isoformat(), p, mod, units, amount))
                    month_units += units
                    month_sales += amount
            erp.execute("INSERT INTO budget VALUES (?,?,?,?)",
                        (y * 100 + m, mod, round(month_sales * rnd.uniform(0.95, 1.08), 2),
                         int(month_units * rnd.uniform(0.95, 1.06))))

    # Fiscal-year summary table (FY ends in June), reported per whole fiscal year.
    for fy in (2025, 2026):
        for p in PROGRAMS:
            enrolled = int(census[p] * rnd.uniform(1.25, 1.4) * (0.94 if fy == 2025 else 1.0))
            tuition = round(enrolled * rnd.uniform(13200, 14600) * (0.97 if fy == 2025 else 1.0), 2)
            sis.execute("INSERT INTO fy_enrollment_summary VALUES (?,?,?,?)", (fy, p, enrolled, tuition))

    sis.commit(); erp.commit()
    sis.close(); erp.close()
    return sis_p, erp_p


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data"))
    a = ap.parse_args()
    for p in build(Path(a.out)):
        print("wrote", p)
