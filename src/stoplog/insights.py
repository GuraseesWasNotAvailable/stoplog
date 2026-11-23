"""Inferences from the running sheet.

Numbers are computed here, in code, from the Log tab (so a human's correction in the sheet
flows straight into the insights). The LLM only turns those numbers into plain-language
findings, and any finding that quotes a number not present in the computed facts is dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import PlantConfig
from .models import Insight, Narrative
from .normalize import parse_clock, parse_sheet_date
from .store import InsightsGrid

TREND_WINDOW_DAYS = 7
MIN_STOPS_FOR_NARRATIVE = 5

NARRATIVE_SYSTEM = """\
You are a plant reliability engineer writing for a plant head who has two minutes. You get \
FACTS computed from a factory's stop log (minutes lost, stop counts, Pareto of causes, \
splits by machine, shift, line and hour into the shift, trends, data quality).

Write 3 to 6 findings, most minutes lost first. Each finding states what the data shows, \
with its numbers. Each action is one concrete next step that follows from that finding: \
what to check, measure or fix first, and where.

Hard rules:
- Use only numbers that appear in FACTS. You may round them. Do not compute new numbers \
(no new percentages, ratios, hours or money).
- Report time in minutes, as FACTS does.
- Do not guess causes the data does not show; say what to check instead.
- If more than 20% of rows need review, include one finding about data quality.
- No filler, no generic advice, no headings."""


# ---------------------------------------------------------------- parsing the Log tab


@dataclass
class Row:
    shift_date: date | None
    shift: str
    line: str
    machine: str
    start: time | None
    start_at: datetime | None
    duration: float | None
    category: str
    reason: str
    needs_review: bool


def _to_float(v) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def _to_date(v) -> date | None:
    f = _to_float(v)
    if f is not None and f > 30000:  # a Google Sheets date serial (someone typed a date)
        return date(1899, 12, 30) + timedelta(days=int(f))
    s = str(v or "").strip()
    return parse_sheet_date(s, s, "DD/MM/YYYY") if s else None


def _to_time(v) -> time | None:
    f = _to_float(v)
    if f is not None and 0 <= f < 1 and "." in str(v):  # a Google Sheets time fraction
        m = round(f * 1440) % 1440
        return time(m // 60, m % 60)
    t, _ = parse_clock(str(v or ""))
    return t


def _to_datetime(v) -> datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_rows(records: list[dict[str, str]]) -> list[Row]:
    rows = []
    for r in records:
        d = _to_date(r.get("shift_date"))
        t = _to_time(r.get("start"))
        start_at = _to_datetime(r.get("start_at")) or (datetime.combine(d, t) if d and t else None)
        rows.append(
            Row(
                shift_date=d,
                shift=str(r.get("shift") or "").strip() or "?",
                line=str(r.get("line") or "").strip() or "unknown line",
                machine=str(r.get("machine") or "").strip() or "unknown machine",
                start=t,
                start_at=start_at,
                duration=_to_float(r.get("duration_min")),
                category=str(r.get("category") or "").strip() or "unclassified",
                reason=str(r.get("reason_group") or "").strip().lower() or "unspecified",
                needs_review=str(r.get("needs_review") or "").strip().lower() in ("yes", "true", "1", "y"),
            )
        )
    return rows


# ---------------------------------------------------------------- facts


def _r(x: float, nd: int = 1) -> float:
    v = round(float(x), nd)
    return int(v) if float(v).is_integer() else v


def _pct(part: float, whole: float) -> float:
    return _r(100.0 * part / whole) if whole else 0


PLANNED = "planned"  # breaks, cleaning, planned maintenance: not a loss to fix (OEE convention)


def compute_facts(rows: list[Row], plant: PlantConfig) -> dict:
    all_rows = rows
    timed_all = [r for r in all_rows if r.duration is not None]
    planned_min = sum(r.duration for r in timed_all if r.category == PLANNED)
    instances = {(r.shift_date, r.shift, r.line) for r in all_rows}
    # Every loss figure below is on unplanned stops only; planned time is reported separately.
    rows = [r for r in all_rows if r.category != PLANNED]
    timed = [r for r in rows if r.duration is not None]
    total_min = sum(r.duration for r in timed)
    shift_len = {s.name: s.minutes for s in plant.shifts}
    logged_min = sum(shift_len.get(shift, 480) for (_, shift, _) in instances)
    dates = sorted({r.shift_date for r in all_rows if r.shift_date})

    facts: dict = {
        "period": {
            "from": dates[0].isoformat() if dates else None,
            "to": dates[-1].isoformat() if dates else None,
            "days_logged": len(dates),
            "shifts_logged": len(instances),
            "lines": sorted({r.line for r in all_rows}),
        },
        "minor_stop_rule": f"equipment stops under {_r(plant.minor_stop_max_min)} min are minor stops",
        "scope": "loss figures cover unplanned stops; planned stops (breaks, cleaning, planned maintenance) are excluded",
        "totals": {
            "stops": len(rows),
            "stops_with_minutes": len(timed),
            "downtime_min": _r(total_min),
            "logged_shift_min": logged_min,
            "downtime_pct_of_logged_time": _pct(total_min, logged_min),
            "avg_stop_min": _r(total_min / len(timed)) if timed else 0,
            "planned_stops": len(all_rows) - len(rows),
            "planned_min": _r(planned_min),
        },
    }
    if timed:
        longest = max(timed, key=lambda r: r.duration)
        facts["totals"]["longest_stop"] = {
            "minutes": _r(longest.duration), "machine": longest.machine, "reason": longest.reason,
            "date": longest.shift_date.isoformat() if longest.shift_date else None, "shift": longest.shift,
        }

    # by category (planned shown too, without a share of the losses)
    cat_n, cat_m = Counter(), defaultdict(float)
    for r in all_rows:
        cat_n[r.category] += 1
        cat_m[r.category] += r.duration or 0
    facts["by_category"] = [
        {"category": c, "stops": cat_n[c], "minutes": _r(cat_m[c]),
         "pct_of_downtime": None if c == PLANNED else _pct(cat_m[c], total_min)}
        for c in sorted(cat_n, key=lambda c: (c == PLANNED, -cat_m[c], -cat_n[c]))
    ]

    # Pareto by reason
    rn, rm = Counter(), defaultdict(float)
    for r in rows:
        rn[r.reason] += 1
        rm[r.reason] += r.duration or 0
    ordered = sorted(rn, key=lambda k: (-rm[k], -rn[k], k))
    cum, pareto, vital = 0.0, [], []
    for k in ordered:
        before = _pct(cum, total_min)
        cum += rm[k]
        pareto.append({"reason": k, "stops": rn[k], "minutes": _r(rm[k]),
                       "pct_of_downtime": _pct(rm[k], total_min), "cumulative_pct": _pct(cum, total_min)})
        if total_min and before < 80:
            vital.append(k)
    facts["pareto"] = pareto[:10]
    facts["vital_few"] = {
        "reasons": vital,
        "pct_of_downtime": _pct(sum(rm[k] for k in vital), total_min),
    }

    # minor stops
    minor = [r for r in rows if r.category == "minor_stop"]
    mn, mm = Counter(), defaultdict(float)
    for r in minor:
        mn[r.reason] += 1
        mm[r.reason] += r.duration or 0
    minor_min = sum(r.duration or 0 for r in minor)
    facts["minor_stops"] = {
        "stops": len(minor),
        "minutes": _r(minor_min),
        "pct_of_stops": _pct(len(minor), len(rows)),
        "pct_of_downtime": _pct(minor_min, total_min),
        "per_shift": _r(len(minor) / len(instances)) if instances else 0,
        "top_by_count": [{"reason": k, "stops": mn[k], "minutes": _r(mm[k])}
                         for k in sorted(mn, key=lambda k: (-mn[k], -mm[k], k))[:8]],
    }

    # by machine
    by_m: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_m[r.machine].append(r)
    machines = []
    for name, rs in by_m.items():
        mins = sum(r.duration or 0 for r in rs)
        n_t = sum(1 for r in rs if r.duration is not None)
        top = Counter(r.reason for r in rs).most_common(1)[0][0]
        machines.append({"machine": name, "stops": len(rs), "minutes": _r(mins),
                         "avg_stop_min": _r(mins / n_t) if n_t else 0, "top_reason": top})
    facts["by_machine"] = sorted(machines, key=lambda m: (-m["minutes"], -m["stops"]))[:10]

    facts["by_shift"] = _split(rows, instances, lambda r: r.shift, "shift")
    facts["by_line"] = _split(rows, instances, lambda r: r.line, "line")

    # hour into the shift
    hours = defaultdict(lambda: [0, 0.0])
    for r in rows:
        s = plant.shift_by_name(r.shift)
        if not (s and r.start and r.shift_date):
            continue
        start_dt = r.start_at or datetime.combine(r.shift_date, r.start)
        shift_start = datetime.combine(r.shift_date, s.start)
        h = int((start_dt - shift_start).total_seconds() // 3600) + 1
        if 1 <= h <= s.minutes // 60 + 1:
            hours[h][0] += 1
            hours[h][1] += r.duration or 0
    facts["by_hour_into_shift"] = [{"hour": h, "stops": v[0], "minutes": _r(v[1])} for h, v in sorted(hours.items())]

    # trend
    daily = defaultdict(lambda: [0, 0.0])
    for r in rows:
        if r.shift_date:
            daily[r.shift_date][0] += 1
            daily[r.shift_date][1] += r.duration or 0
    facts["daily"] = [{"date": d.isoformat(), "stops": v[0], "minutes": _r(v[1])}
                      for d, v in sorted(daily.items())[-14:]]
    facts["recent_vs_before"] = _trend(rows, dates, [p["reason"] for p in pareto[:5]])

    n_review = sum(1 for r in all_rows if r.needs_review)
    facts["data_quality"] = {
        "rows": len(all_rows),
        "needs_review": n_review,
        "pct_needs_review": _pct(n_review, len(all_rows)),
        "rows_without_minutes": len(all_rows) - len(timed_all),
    }
    return facts


def _split(rows, instances, key, label):
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for name, rs in groups.items():
        n_inst = len({i for i in instances if (i[1] if label == "shift" else i[2]) == name})
        mins = sum(r.duration or 0 for r in rs)
        out.append({label: name, "shifts_logged": n_inst, "stops": len(rs), "minutes": _r(mins),
                    "stops_per_shift": _r(len(rs) / n_inst) if n_inst else 0,
                    "minutes_per_shift": _r(mins / n_inst) if n_inst else 0})
    return sorted(out, key=lambda x: (-x["minutes_per_shift"], str(x[label])))


def _trend(rows: list[Row], dates: list[date], reasons: list[str]):
    if len(dates) < 6:
        return None
    cutoff = dates[-1] - timedelta(days=TREND_WINDOW_DAYS - 1)
    recent = [r for r in rows if r.shift_date and r.shift_date >= cutoff]
    before = [r for r in rows if r.shift_date and r.shift_date < cutoff]
    inst_recent = {(r.shift_date, r.shift, r.line) for r in recent}
    inst_before = {(r.shift_date, r.shift, r.line) for r in before}
    if len(inst_recent) < 3 or len(inst_before) < 3:
        return None
    out = []
    for reason in reasons:
        b = sum(r.duration or 0 for r in before if r.reason == reason) / len(inst_before)
        c = sum(r.duration or 0 for r in recent if r.reason == reason) / len(inst_recent)
        out.append({"reason": reason, "before_min_per_shift": _r(b), "last_7_days_min_per_shift": _r(c),
                    "change_pct": _pct(c - b, b) if b else None})
    return {"last_7_days_from": cutoff.isoformat(), "by_reason": out}


# ---------------------------------------------------------------- narrative + number guard

_DATE_OR_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{1,2}:\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    text = _DATE_OR_TIME.sub(" ", text)
    return [float(m.group().replace(",", "")) for m in _NUMBER.finditer(text)]


def _fact_numbers(obj) -> list[float]:
    out: list[float] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        out.extend(float(m.group()) for m in re.finditer(r"\d+(?:\.\d+)?", obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_fact_numbers(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_fact_numbers(v))
    return out


def number_is_supported(n: float, known: list[float]) -> bool:
    n = float(n)  # int.is_integer() only exists from Python 3.12; Pi OS Bookworm ships 3.11
    if n.is_integer() and 1 <= n <= 5:  # "top 3", "2 machines"
        return True
    return any(abs(n - f) <= max(0.51, 0.015 * abs(f)) for f in known)


def guard_insights(insights: list[Insight], facts: dict) -> tuple[list[Insight], int]:
    known = _fact_numbers(facts)
    kept, dropped = [], 0
    for ins in insights:
        nums = _numbers(ins.finding) + _numbers(ins.action)
        if all(number_is_supported(n, known) for n in nums):
            kept.append(ins)
        else:
            dropped += 1
    return kept, dropped


def facts_digest(facts: dict) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


def write_narrative(facts: dict, llm, model: str, cache_dir: Path | None = None) -> tuple[list[Insight], str]:
    """Returns (findings, note). Never raises: insights tables are written even if this fails."""
    if facts["totals"]["stops"] < MIN_STOPS_FOR_NARRATIVE:
        return [], f"Findings appear once at least {MIN_STOPS_FOR_NARRATIVE} stops are logged."
    digest = facts_digest(facts)
    cache_file = cache_dir / "narrative_cache.json" if cache_dir else None
    if cache_file and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("digest") == digest:
                return [Insight(**i) for i in cached["insights"]], cached.get("note", "")
        except Exception:
            pass
    if llm is None:
        return [], "Written findings are off (no LLM configured)."
    try:
        result: Narrative = llm.structured(
            model=model,
            system=NARRATIVE_SYSTEM,
            text="FACTS:\n" + json.dumps(facts, indent=1, default=str),
            schema=Narrative,
            effort="medium",
        )
    except Exception as exc:
        return [], f"Written findings unavailable this update ({type(exc).__name__}). Tables are current."
    kept, dropped = guard_insights(result.insights, facts)
    note = f"{dropped} finding(s) removed because a number did not match the data." if dropped else ""
    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"digest": digest, "insights": [i.model_dump() for i in kept],
                                              "note": note}), encoding="utf-8")
        except OSError:
            pass
    return kept, note


# ---------------------------------------------------------------- rendering


def render_grid(facts: dict, insights: list[Insight], note: str, updated: datetime, plant: PlantConfig) -> InsightsGrid:
    g = InsightsGrid()

    def add(row=(), kind=None):
        idx = len(g.rows)
        g.rows.append(list(row))
        if kind == "title":
            g.title_rows.append(idx)
        elif kind == "section":
            g.section_rows.append(idx)
        elif kind == "header":
            g.header_rows.append(idx)

    p, t = facts["period"], facts["totals"]
    add(["Stop log insights"], "title")
    add(["Updated", updated.strftime("%Y-%m-%d %H:%M"), "Period",
         f"{p['from']} to {p['to']}" if p["from"] else "-", "Shifts logged", p["shifts_logged"]])
    add()
    if t["stops"] + t["planned_stops"] == 0:
        add(["No stops logged yet. Drop a photo of a stop sheet into the inbox folder."])
        return g

    add(["Headline numbers (unplanned stops)"], "section")
    add(["Stops", "Downtime (min)", "Downtime % of logged shift time", "Average stop (min)",
         "Minor stops", "Planned time (min)", "Rows to review"], "header")
    add([t["stops"], t["downtime_min"], t["downtime_pct_of_logged_time"], t["avg_stop_min"],
         facts["minor_stops"]["stops"], t["planned_min"], facts["data_quality"]["needs_review"]])
    add()

    add(["What the data says"], "section")
    if insights:
        add(["#", "Finding", "Suggested next step"], "header")
        for i, ins in enumerate(insights, 1):
            add([i, ins.finding, ins.action])
    if note:
        add([note])
    add()

    add(["Top loss causes (Pareto by minutes)"], "section")
    add(["Reason", "Stops", "Minutes", "% of downtime", "Cumulative %"], "header")
    for x in facts["pareto"]:
        add([x["reason"], x["stops"], x["minutes"], x["pct_of_downtime"], x["cumulative_pct"]])
    vf = facts["vital_few"]
    if vf["reasons"]:
        add([f"{len(vf['reasons'])} cause(s) make {vf['pct_of_downtime']}% of downtime: " + ", ".join(vf["reasons"])])
    add()

    ms = facts["minor_stops"]
    add([f"Minor stops (under {_r(plant.minor_stop_max_min)} min), most frequent first"], "section")
    add(["Reason", "Stops", "Minutes"], "header")
    for x in ms["top_by_count"]:
        add([x["reason"], x["stops"], x["minutes"]])
    add([f"{ms['stops']} minor stops, {ms['pct_of_stops']}% of all stops, {ms['per_shift']} per shift"])
    add()

    add(["Stops by category"], "section")
    add(["Category", "Stops", "Minutes", "% of unplanned downtime"], "header")
    for x in facts["by_category"]:
        pct = "not a loss" if x["pct_of_downtime"] is None else x["pct_of_downtime"]
        add([x["category"].replace("_", " "), x["stops"], x["minutes"], pct])
    add()

    add(["By machine"], "section")
    add(["Machine", "Stops", "Minutes", "Average stop (min)", "Most common reason"], "header")
    for x in facts["by_machine"]:
        add([x["machine"], x["stops"], x["minutes"], x["avg_stop_min"], x["top_reason"]])
    add()

    for key, title in (("by_shift", "Shift"), ("by_line", "Line")):
        add([f"By {title.lower()}"], "section")
        add([title, "Shifts logged", "Stops", "Minutes", "Stops per shift", "Minutes per shift"], "header")
        for x in facts[key]:
            add([x[key[3:]], x["shifts_logged"], x["stops"], x["minutes"], x["stops_per_shift"], x["minutes_per_shift"]])
        add()

    if facts["by_hour_into_shift"]:
        add(["Stops by hour into the shift"], "section")
        add(["Hour", "Stops", "Minutes"], "header")
        for x in facts["by_hour_into_shift"]:
            add([x["hour"], x["stops"], x["minutes"]])
        add()

    add(["Daily downtime (last 14 days logged)"], "section")
    add(["Date", "Stops", "Minutes"], "header")
    for x in facts["daily"]:
        add([x["date"], x["stops"], x["minutes"]])
    add()

    trend = facts.get("recent_vs_before")
    if trend:
        add([f"Last 7 days (from {trend['last_7_days_from']}) vs before, minutes per shift"], "section")
        add(["Reason", "Before", "Last 7 days", "Change %"], "header")
        for x in trend["by_reason"]:
            add([x["reason"], x["before_min_per_shift"], x["last_7_days_min_per_shift"],
                 "" if x["change_pct"] is None else x["change_pct"]])
        add()

    dq = facts["data_quality"]
    add(["Data quality"], "section")
    add([f"{dq['needs_review']} of {dq['rows']} rows need review ({dq['pct_needs_review']}%). "
         f"{dq['rows_without_minutes']} rows have no minutes. Filter the Log tab on needs_review, "
         f"fix the cells, and the next update recalculates."])
    add()
    add(["How this is calculated"], "section")
    add([f"Minor stop = equipment stop under {_r(plant.minor_stop_max_min)} min; longer equipment stops are "
         "breakdowns, whatever box was ticked. Changeover, planned, material, quality, utility and manpower "
         "stops keep their type."])
    add(["Loss figures use unplanned stops only. Planned stops (breaks, cleaning, planned maintenance) "
         "are listed under categories but are not ranked as losses."])
    add(["Downtime % = unplanned stop minutes / (shifts logged x shift length). Stops on parallel "
         "machines can overlap."])
    add(["Findings are written by the AI from these tables only; any finding quoting a number not in "
         "the tables is removed."])
    return g


def build_insights(records: list[dict[str, str]], plant: PlantConfig, llm=None, model: str = "",
                   cache_dir: Path | None = None, now: datetime | None = None) -> tuple[InsightsGrid, dict]:
    rows = parse_rows(records)
    facts = compute_facts(rows, plant)
    insights, note = write_narrative(facts, llm, model, cache_dir) if rows else ([], "")
    return render_grid(facts, insights, note, now or datetime.now(), plant), facts
