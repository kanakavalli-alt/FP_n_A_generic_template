"""Grounding guard: honest restatements pass, anything else is withheld, and
every passing number is attributed to evidence."""
from datetime import date

from fpa_agent.grounding import guard
from fpa_agent.grounding.guard import Capture

TODAY = date(2026, 10, 15)
KW = {"channel", "program", "driver"}


def caps(*numbers, labels=(), ev="ev_1", exploratory=False, returned=True):
    nums = set()
    for n in numbers:
        nums |= guard.extract_numbers(str(n))
    return Capture(numbers=sorted(nums), labels=[l.lower() for l in labels], evidence_ids=[ev],
                   tool="t", exploratory=exploratory, returned_data=returned)


def check(text, captures, user=()):
    return guard.check(text, captures, set(user), KW, today=TODAY)


def test_exact_rounded_scaled_percent_and_combination_pass():
    c = [caps("1236", "1211", "25", "659542.71", "0.1216", "80", "-22")]
    v = check("Starts were 1,236 vs 1,211 (+25). Revenue $659.5K ($0.66M). Conversion 12.2%. "
              "Volume and mix together added 58.", c)
    assert v.ok, v.message()
    assert v.attribution["1236"]["evidence_ids"] == ["ev_1"]
    assert v.attribution["58"]["kind"] == "combination"


def test_invented_number_blocked():
    v = check("Starts were 1,236, up 4.7% on last year.", [caps("1236", "1211", "2.06")])
    assert not v.ok and "4.7" in v.offending_numbers


def test_quarter_and_fy_labels_and_list_markers_ignored():
    v = check("1. In FY2026 Q3 starts were 1,236.\n2. See Q4 next.", [caps("1236")])
    assert v.ok, v.message()


def test_user_numbers_allowed():
    v = check("You mentioned a 10% target; actual growth was 2.06%.", [caps("2.06")], user={"10"})
    assert v.ok


def test_fabricated_table_row_blocked():
    text = "| Channel | Starts |\n|---|---|\n| Organic | 120 |\n| Search | 80 |"
    v = check(text, [caps("120", "80", labels=["Organic", "Paid Search"])])
    # "search" is a substring of "paid search" -> allowed; a truly new label is not
    assert v.ok
    v = check(text.replace("Search", "Social Media"), [caps("120", "80", labels=["Organic", "Paid Search"])])
    assert not v.ok and v.offending_labels == ["Social Media"]


def test_exploratory_numbers_need_a_label():
    c = [caps("1236"), caps("4321", ev="ev_x", exploratory=True)]
    assert not check("Starts 1,236; the raw table shows 4,321 rows.", c).ok
    assert check("Starts 1,236; an exploratory query shows 4,321 rows.", c).ok


def test_claiming_no_data_after_exploratory_rows_blocked():
    c = [caps("12", exploratory=True, returned=True)]
    v = check("Unfortunately no data is available for that.", c)
    assert v.contradicts_data and not v.ok


def test_scale_suffix_not_truncated():
    assert "227.95" in guard.extract_numbers("$227.95K")


def test_alphanumeric_tokens_not_numbers():
    assert guard.extract_numbers("B2B and 3PL partners") == set()
