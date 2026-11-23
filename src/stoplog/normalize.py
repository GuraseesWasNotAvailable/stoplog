"""Turn what the model read off a sheet into clean, consistent stop rows.

Everything here is deterministic. The model transcribes; this module applies the plant's
rules: shift timing, 12h/24h repair, durations, one consistent minor-stop/breakdown split,
reason grouping, duplicate fingerprints, and "needs review" flags.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .config import PlantConfig, Shift
from .models import ExtractedSheet, ExtractedStop, StopEvent

DURATION_MISMATCH_MIN = 2.0  # written vs clock-computed minutes may differ by this much
START_GRACE_MIN = 30  # a stop may be written a little before/after the shift window

_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?:\s*[:.hH]\s*(\d{2}))?(?:\s*:\s*\d{2})?\s*([aApP])?\.?\s*[mM]?\.?\s*(?:hrs?|h)?\s*$"
)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "general": 1}


# ---------------------------------------------------------------- small parsers


def parse_clock(value: str | None) -> tuple[time | None, bool]:
    """Parse a written time. Returns (time, had_am_pm)."""
    if value is None:
        return None, False
    s = str(value).strip()
    if not s:
        return None, False
    m4 = re.fullmatch(r"(\d{2})(\d{2})", s)  # "1405"
    if m4:
        h, mi, ampm = int(m4.group(1)), int(m4.group(2)), None
    else:
        m = _TIME_RE.match(s)
        if not m:
            return None, False
        h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= h <= 12:
            return None, False
        h = h % 12 + (12 if ampm.lower() == "p" else 0)
    if h == 24 and mi == 0:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None, False
    return time(h, mi), bool(ampm)


def parse_sheet_date(iso: str | None, as_written: str | None, date_format: str) -> date | None:
    if iso:
        try:
            return date.fromisoformat(iso.strip()[:10])
        except ValueError:
            pass
    if not as_written:
        return None
    parts = re.findall(r"\d+", as_written)
    if len(parts) < 3:
        return None
    a, b, c = (int(x) for x in parts[:3])
    try:
        if len(parts[0]) == 4:  # YYYY-MM-DD
            return date(a, b, c)
        year = c if c >= 100 else 2000 + c
        if date_format.startswith("MM"):
            return date(year, a, b)
        return date(year, b, a)  # DD/MM/YYYY, the Indian default
    except ValueError:
        return None


def _norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w/]+", " ", (value or "").lower())).strip()


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text) is not None


def resolve_shift(raw: str | None, plant: PlantConfig) -> Shift | None:
    if not raw:
        return None
    r = _norm_text(raw)
    r = re.sub(r"\bshift\b", "", r).strip()
    direct = plant.shift_by_name(r)
    if direct:
        return direct
    idx = None
    if r.isdigit():
        idx = int(r)
    elif r in _ROMAN:
        idx = _ROMAN[r]
    elif r in _ORDINAL:
        idx = _ORDINAL[r]
    if idx is not None and 1 <= idx <= len(plant.shifts):
        return plant.shifts[idx - 1]
    probe = {"morning": time(8, 0), "day": time(10, 0), "afternoon": time(16, 0),
             "evening": time(18, 0), "night": time(2, 0)}.get(r)
    if probe:
        for s in plant.shifts:
            if s.contains(probe):
                return s
    return None


def shift_for_time(t: time, plant: PlantConfig) -> Shift | None:
    for s in plant.shifts:
        if s.contains(t):
            return s
    return None


def normalize_line(raw: str | None, plant: PlantConfig) -> str:
    if not raw or not str(raw).strip():
        return ""
    s = re.sub(r"\s+", " ", str(raw)).strip()
    for known in plant.lines:
        if known.lower() == s.lower():
            return known
    m = re.fullmatch(r"(?i)(?:line|ln|l)?\s*[-#:.]?\s*(\d+)", s)
    if m:
        candidate = f"Line {int(m.group(1))}"
        for known in plant.lines:
            if known.lower() == candidate.lower():
                return known
        return candidate
    return s


def normalize_machine(raw: str | None, plant: PlantConfig) -> str:
    if not raw or not str(raw).strip():
        return ""
    s = re.sub(r"\s+", " ", str(raw)).strip()
    for known in plant.machines:
        if known.lower() == s.lower():
            return known
    if (s.islower() or (s.isupper() and len(s) > 4)):
        return s.title()
    return s


def normalize_reason_group(raw: str | None, fallback_text: str | None, plant: PlantConfig) -> str:
    text = _norm_text(raw)
    if text:
        for group, synonyms in plant.reason_groups.items():
            if text == group or text in synonyms:
                return group
        return text
    # The model gave no group: try to map the free text onto a known group.
    free = _norm_text(fallback_text)
    if free:
        for group, synonyms in plant.reason_groups.items():
            for phrase in [group, *synonyms]:
                if _has_phrase(free, phrase):
                    return group
    return "unspecified"


def classify(
    category_written: str | None,
    reason_text: str,
    duration: float | None,
    plant: PlantConfig,
) -> str:
    """One consistent rule for every row.

    Planned / changeover / material / quality / utility / manpower stops keep that category.
    Every other (equipment) stop is a minor stop if shorter than `minor_stop_max_min`, else a
    breakdown, whatever the operator ticked. That removes the "10-30 min stops are logged as
    breakdown or minor stop at each person's discretion" problem.
    """
    written = _norm_text(category_written)
    category = None
    if written:
        for cat, synonyms in plant.category_synonyms.items():
            if written == cat.replace("_", " ") or written in synonyms:
                category = cat
                break
        if category is None:
            for cat, synonyms in plant.category_synonyms.items():
                if any(len(p) > 3 and _has_phrase(written, p) for p in synonyms):
                    category = cat
                    break
    if category is None:
        reason = _norm_text(reason_text)
        for cat, phrases in plant.reason_hints.items():
            if any(_has_phrase(reason, p) for p in phrases):
                category = cat
                break
    if category in (None, "breakdown", "minor_stop"):
        if duration is None:
            return category or "unclassified"
        return "minor_stop" if duration < plant.minor_stop_max_min else "breakdown"
    return category


# ---------------------------------------------------------------- per-row logic


@dataclass
class _Notes:
    review: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "; ".join(self.review + self.info)


def _fit_to_shift(t: time, had_ampm: bool, shift: Shift | None, notes: _Notes, label: str) -> time:
    """Repair a 12-hour time written without AM/PM ("2:05" on a 14:00-22:00 shift)."""
    if shift is None or had_ampm or shift.contains(t, START_GRACE_MIN):
        return t
    if t.hour < 12:
        shifted = time(t.hour + 12, t.minute)
        if shift.contains(shifted, START_GRACE_MIN):
            notes.info.append(f"{label} read as {shifted:%H:%M} for shift {shift.name}")
            return shifted
    if t.hour == 12:
        shifted = time(0, t.minute)
        if shift.contains(shifted, START_GRACE_MIN):
            notes.info.append(f"{label} read as {shifted:%H:%M} for shift {shift.name}")
            return shifted
    return t


def _anchor(shift_date: date, t: time, shift: Shift | None) -> datetime:
    """Calendar datetime of a clock time that belongs to the shift starting on `shift_date`."""
    dt = datetime.combine(shift_date, t)
    if shift is not None and shift.crosses_midnight:
        minutes = t.hour * 60 + t.minute
        if minutes < shift.start_min and minutes <= shift.end_min + 120:
            dt += timedelta(days=1)
    return dt


def _fmt_num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def event_id(*parts: object) -> str:
    key = "|".join("" if p is None else str(p).strip().lower() for p in parts)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class NormalizedSheet:
    events: list[StopEvent]
    shift_date: str
    shift: str
    line: str
    warnings: list[str]


def normalize_sheet(
    sheet: ExtractedSheet,
    plant: PlantConfig,
    *,
    photo_name: str,
    photo_sha: str,
    photo_time: datetime | None,
    now: datetime | None = None,
) -> NormalizedSheet:
    now = now or datetime.now()
    warnings: list[str] = [f"unreadable: {u}" for u in sheet.unreadable if u and u.strip()]

    # ---- sheet-level: shift, date, line
    shift = resolve_shift(sheet.shift, plant)
    if shift is None:
        for stop in sheet.stops:
            t, _ = parse_clock(stop.start_time)
            if t is not None:
                shift = shift_for_time(t, plant)
                if shift is not None:
                    warnings.append(f"shift not readable on sheet; inferred {shift.name} from stop times")
                break
    if shift is None and sheet.shift:
        warnings.append(f"shift {sheet.shift!r} does not match any configured shift")

    sheet_date = parse_sheet_date(sheet.sheet_date, sheet.sheet_date_as_written, plant.date_format)
    date_from_photo = False
    if sheet_date is None:
        ref = photo_time or now
        sheet_date = ref.date()
        if shift is not None and shift.crosses_midnight:
            minutes = ref.hour * 60 + ref.minute
            if minutes < shift.start_min and minutes <= shift.end_min + 180:
                sheet_date -= timedelta(days=1)  # night shift that started yesterday
        date_from_photo = True
        warnings.append(f"no date on sheet; used photo date {sheet_date.isoformat()}")

    line = normalize_line(sheet.line, plant)
    if not line:
        warnings.append("line not written on sheet")

    shift_name = shift.name if shift else (sheet.shift or "").strip()
    logged_at = now.isoformat(timespec="seconds")
    events: list[StopEvent] = []
    seen_untimed: dict[tuple, int] = {}

    for stop in sheet.stops:
        count = max(1, int(stop.occurrences or 1))
        for k in range(count):
            ev = _normalize_stop(
                stop, plant, shift, shift_name, sheet_date, line, sheet.sku,
                tally=count, photo_name=photo_name, photo_sha=photo_sha,
                logged_at=logged_at, date_from_photo=date_from_photo,
            )
            # Fingerprint: the same stop read from two photos of the same sheet gets the
            # same id, so it is logged once. Untimed rows (tallies of minor stops) are told
            # apart by their order of appearance.
            if ev.start:
                ev.event_id = event_id(ev.shift_date, ev.shift, ev.line, ev.machine, ev.start, ev.reason_group)
            else:
                key = (ev.shift_date, ev.shift, ev.line, ev.machine, ev.reason_group, ev.duration_min)
                n = seen_untimed.get(key, 0)
                seen_untimed[key] = n + 1
                ev.event_id = event_id(*key, f"#{n}")
            events.append(ev)

    return NormalizedSheet(
        events=events,
        shift_date=sheet_date.isoformat(),
        shift=shift_name,
        line=line,
        warnings=warnings,
    )


def _normalize_stop(
    stop: ExtractedStop,
    plant: PlantConfig,
    shift: Shift | None,
    shift_name: str,
    shift_date: date,
    line: str,
    sheet_sku: str | None,
    *,
    tally: int,
    photo_name: str,
    photo_sha: str,
    logged_at: str,
    date_from_photo: bool,
) -> StopEvent:
    notes = _Notes()
    if stop.confidence == "low":
        notes.review.append("low confidence read" + (f": {stop.note}" if stop.note else ""))
    elif stop.note:
        notes.info.append(stop.note)
    if date_from_photo:
        notes.info.append("date from photo time")

    start_t, start_ampm = parse_clock(stop.start_time)
    end_t, end_ampm = parse_clock(stop.end_time)
    if stop.start_time and start_t is None:
        notes.review.append(f"start time {stop.start_time!r} not understood")
    if stop.end_time and end_t is None:
        notes.review.append(f"end time {stop.end_time!r} not understood")
    if start_t is not None:
        start_t = _fit_to_shift(start_t, start_ampm, shift, notes, "start")
    if end_t is not None:
        end_t = _fit_to_shift(end_t, end_ampm, shift, notes, "end")

    start_dt = _anchor(shift_date, start_t, shift) if start_t else None
    end_dt = _anchor(shift_date, end_t, shift) if end_t else None
    if start_dt and end_dt and end_dt < start_dt:
        end_dt += timedelta(days=1)

    computed = (end_dt - start_dt).total_seconds() / 60 if start_dt and end_dt else None
    written = stop.duration_min if stop.duration_min is not None and stop.duration_min >= 0 else None
    if tally > 1:
        # A tally row: the written minutes are the total for all of them.
        written = written / tally if written is not None else None
        computed = None
        notes.info.append(f"one of {tally} stops tallied on one row")
    if computed is not None and written is not None and abs(computed - written) > DURATION_MISMATCH_MIN:
        notes.review.append(
            f"written {_fmt_num(written)} min but {start_t:%H:%M}-{end_t:%H:%M} is {_fmt_num(computed)} min"
        )
    duration = computed if computed is not None else written
    if duration is None:
        notes.review.append("no duration")
    elif shift is not None and duration > shift.minutes:
        notes.review.append(f"{_fmt_num(duration)} min is longer than the shift")

    if start_t is not None and shift is not None and not shift.contains(start_t, START_GRACE_MIN):
        notes.review.append(f"start {start_t:%H:%M} is outside shift {shift.label()}")

    reason_text = " ".join(x for x in [stop.reason_english, stop.reason_as_written] if x)
    category = classify(stop.category_as_written, reason_text, duration, plant)
    reason_group = normalize_reason_group(stop.reason_group, reason_text, plant)
    if reason_group == "unspecified" and not reason_text:
        notes.review.append("no reason written")

    return StopEvent(
        event_id="",
        shift_date=shift_date.isoformat(),
        shift=shift_name,
        line=line,
        machine=normalize_machine(stop.machine, plant),
        sku=(stop.sku or sheet_sku or "").strip(),
        start=f"{start_t:%H:%M}" if start_t else "",
        end=f"{end_t:%H:%M}" if end_t else "",
        start_at=start_dt.isoformat(timespec="minutes") if start_dt else "",
        duration_min=round(duration, 1) if duration is not None else None,
        category=category,
        reason_group=reason_group,
        reason_detail=(stop.reason_english or "").strip(),
        reason_as_written=(stop.reason_as_written or "").strip(),
        category_as_written=(stop.category_as_written or "").strip(),
        action_taken=(stop.action_taken or "").strip(),
        recorded_by=(stop.recorded_by or "").strip(),
        confidence=stop.confidence,
        needs_review=bool(notes.review),
        review_note=notes.text(),
        photo=photo_name,
        photo_sha=photo_sha,
        logged_at=logged_at,
    )
