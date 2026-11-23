from datetime import date, datetime, timedelta

from conftest import FakeLLM, sample_sheet

from stoplog.insights import (build_insights, compute_facts, guard_insights, number_is_supported,
                              parse_rows, write_narrative)
from stoplog.models import LOG_COLUMNS, Insight, Narrative
from stoplog.normalize import normalize_sheet


def sample_records(plant, shift_date="2026-11-24", shift="B"):
    sheet = sample_sheet()
    sheet.sheet_date, sheet.shift = shift_date, shift
    ev = normalize_sheet(sheet, plant, photo_name="p", photo_sha="s", photo_time=None).events
    return [dict(zip(LOG_COLUMNS, [str(v) for v in e.as_row()])) for e in ev]


def test_facts_from_sample(plant):
    facts = compute_facts(parse_rows(sample_records(plant)), plant)
    t = facts["totals"]
    # the 30-min "tea break + cleaning" row is planned time, not a loss
    assert t["stops"] == 13 and t["downtime_min"] == 148
    assert t["planned_stops"] == 1 and t["planned_min"] == 30
    assert t["logged_shift_min"] == 480 and t["downtime_pct_of_logged_time"] == 30.8
    assert facts["pareto"][0]["reason"] == "sku changeover" and facts["pareto"][0]["minutes"] == 45
    assert "cleaning" not in [p["reason"] for p in facts["pareto"]]
    assert facts["pareto"][-1]["cumulative_pct"] == 100
    planned = [c for c in facts["by_category"] if c["category"] == "planned"][0]
    assert planned["minutes"] == 30 and planned["pct_of_downtime"] is None
    cums = [p["cumulative_pct"] for p in facts["pareto"]]
    assert cums == sorted(cums)
    ms = facts["minor_stops"]
    assert ms["stops"] == 9  # 6+3+6 min stops, 5 tallied, and the 8-min 'BD'
    assert ms["top_by_count"][0] == {"reason": "wrapper jam", "stops": 5, "minutes": 10}
    assert facts["data_quality"] == {"rows": 14, "needs_review": 1, "pct_needs_review": 7.1,
                                     "rows_without_minutes": 0}
    assert facts["recent_vs_before"] is None  # one day of data: no trend yet
    hours = {h["hour"]: h["stops"] for h in facts["by_hour_into_shift"]}
    assert hours[1] == 2 and sum(hours.values()) == 8  # timed, unplanned stops only


def test_trend_needs_two_windows(plant):
    records = []
    start = date(2026, 11, 1)
    for i in range(14):
        records += sample_records(plant, (start + timedelta(days=i)).isoformat())
    facts = compute_facts(parse_rows(records), plant)
    trend = facts["recent_vs_before"]
    assert trend and trend["last_7_days_from"] == "2026-11-08"
    assert all(x["change_pct"] == 0 for x in trend["by_reason"])  # same sheet every day
    assert facts["by_shift"][0]["shifts_logged"] == 14


def test_sheet_serial_dates_and_time_fractions_are_understood(plant):
    rec = dict.fromkeys(LOG_COLUMNS, "")
    rec.update(shift_date=46350.0, start="0.5868055555555556", duration_min="5", shift="B")
    row = parse_rows([rec])[0]
    assert row.shift_date == date(2026, 11, 24)
    assert row.start.strftime("%H:%M") == "14:05"


def test_number_guard():
    facts = {"totals": {"downtime_min": 178, "downtime_pct_of_logged_time": 37.1}, "line": "Line 3"}
    good = Insight(finding="178 min lost on Line 3, 37% of shift time, mostly after 14:00 on 2026-11-24.",
                   action="Check the top 3 causes first.")
    bad = Insight(finding="About 3 hours lost, costing 52000 rupees.", action="Fix it.")
    kept, dropped = guard_insights([good, bad], facts)
    assert kept == [good] and dropped == 1
    assert number_is_supported(37.0, [37.1]) and not number_is_supported(52000, [178])


def test_narrative_is_guarded_and_cached(plant, tmp_path):
    facts = compute_facts(parse_rows(sample_records(plant)), plant)
    llm = FakeLLM(Narrative(insights=[
        Insight(finding="SKU changeover cost 45 min, the largest single cause.", action="Time each changeover step."),
        Insight(finding="Downtime was 99999 min.", action="Panic."),
    ]))
    kept, note = write_narrative(facts, llm, "m", cache_dir=tmp_path)
    assert len(kept) == 1 and "1 finding(s) removed" in note
    assert llm.calls[0]["effort"] == "medium" and "FACTS" in llm.calls[0]["text"]
    kept2, _ = write_narrative(facts, FakeLLM(), "m", cache_dir=tmp_path)  # served from cache, no call
    assert kept2 == kept


def test_narrative_failure_still_returns_tables(plant):
    grid, facts = build_insights(sample_records(plant), plant, llm=FakeLLM(RuntimeError("offline")), model="m",
                                 now=datetime(2026, 11, 24, 22, 0))
    flat = [str(c) for row in grid.rows for c in row]
    assert "Top loss causes (Pareto by minutes)" in flat
    assert any("unavailable" in c for c in flat)
    assert grid.title_rows == [0] and grid.header_rows and grid.section_rows


def test_empty_log(plant):
    grid, facts = build_insights([], plant, now=datetime(2026, 11, 24))
    assert facts["totals"]["stops"] == 0
    assert any("No stops logged yet" in str(r[0]) for r in grid.rows if r)
