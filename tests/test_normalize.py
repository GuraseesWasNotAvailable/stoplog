from datetime import date, datetime, time

import pytest
from conftest import sample_sheet, stop

from stoplog.config import PlantConfig
from stoplog.models import ExtractedSheet
from stoplog.normalize import classify, normalize_line, normalize_sheet, parse_clock, parse_sheet_date, resolve_shift


@pytest.mark.parametrize("raw,expected", [
    ("14:05", time(14, 5)), ("14.05", time(14, 5)), ("1405", time(14, 5)), ("9:5", None),
    ("2:05 pm", time(14, 5)), ("2.05PM", time(14, 5)), ("12:30 am", time(0, 30)), ("12:10 PM", time(12, 10)),
    ("24:00", time(0, 0)), ("25:00", None), ("abc", None), ("", None), (None, None), ("14:05:00", time(14, 5)),
    ("7", time(7, 0)), ("13:00 pm", None),
])
def test_parse_clock(raw, expected):
    assert parse_clock(raw)[0] == expected


def test_parse_sheet_date_prefers_iso_then_day_first():
    assert parse_sheet_date("2026-11-24", None, "DD/MM/YYYY") == date(2026, 11, 24)
    assert parse_sheet_date(None, "05/11/26", "DD/MM/YYYY") == date(2026, 11, 5)
    assert parse_sheet_date(None, "05/11/26", "MM/DD/YYYY") == date(2026, 5, 11)
    assert parse_sheet_date("garbage", "31/02/2026", "DD/MM/YYYY") is None


def test_resolve_shift_variants(plant):
    assert resolve_shift("B", plant).name == "B"
    assert resolve_shift("Shift b", plant).name == "B"
    assert resolve_shift("II", plant).name == "B"
    assert resolve_shift("3", plant).name == "C"
    assert resolve_shift("night", plant).name == "C"
    assert resolve_shift("Z", plant) is None


def test_normalize_line(plant):
    assert normalize_line("L-3", plant) == "Line 3"
    assert normalize_line("line 03", plant) == "Line 3"
    assert normalize_line("Packing hall", plant) == "Packing hall"


def test_classify_applies_one_rule(plant):
    assert classify("BD", "belt slipping", 8, plant) == "minor_stop"  # ticked BD, but 8 < 10 min
    assert classify("MS", "jam", 25, plant) == "breakdown"  # ticked MS, but 25 min
    assert classify("CO", "", 45, plant) == "changeover"
    assert classify(None, "power cut from grid", 20, plant) == "utility"
    assert classify(None, "film break at jaw", 20, plant) == "breakdown"  # 'break' is not a tea break
    assert classify("MAT", "film khatam", 15, plant) == "material"
    assert classify(None, "jam", None, plant) == "unclassified"


def _by_machine_start(events):
    return {(e.machine, e.start): e for e in events}


def test_sample_sheet_normalises_as_expected(plant):
    norm = normalize_sheet(sample_sheet(), plant, photo_name="p.jpg", photo_sha="abc",
                           photo_time=datetime(2026, 11, 24, 22, 5), now=datetime(2026, 11, 24, 22, 6))
    ev = norm.events
    assert len(ev) == 14  # 9 timed rows + a tally row of 5
    assert {e.shift_date for e in ev} == {"2026-11-24"}
    assert {e.shift for e in ev} == {"B"} and {e.line for e in ev} == {"Line 3"}
    assert len({e.event_id for e in ev}) == 14  # tally stops stay distinct

    rows = _by_machine_start(ev)
    cartoner = rows[("Cartoner", "16:10")]  # "4:10" written on a 14:00-22:00 shift
    assert cartoner.end == "16:22" and cartoner.duration_min == 12 and not cartoner.needs_review
    assert cartoner.category == "breakdown"

    film = rows[("Wrapper", "18:05")]
    assert film.duration_min == 20 and film.needs_review  # written 15, clock says 20
    assert "written 15 min but 18:05-18:25 is 20 min" in film.review_note
    assert film.category == "material" and film.reason_group == "no material"

    assert rows[("Conveyor", "19:10")].category == "minor_stop"  # ticked BD, 8 min
    assert rows[("Wrapper", "14:52")].category == "breakdown"
    assert rows[("Line", "17:00")].category == "planned"
    assert rows[("Wrapper", "20:00")].category == "changeover"

    tally = [e for e in ev if e.reason_group == "wrapper jam"]
    assert len(tally) == 5 and all(e.duration_min == 2 and e.category == "minor_stop" for e in tally)
    assert sum(e.duration_min for e in ev) == 178


def test_same_sheet_twice_gives_same_ids(plant):
    a = normalize_sheet(sample_sheet(), plant, photo_name="a.jpg", photo_sha="1", photo_time=None)
    b = normalize_sheet(sample_sheet(), plant, photo_name="b.jpg", photo_sha="2", photo_time=None)
    assert [e.event_id for e in a.events] == [e.event_id for e in b.events]


def test_night_shift_after_midnight_is_next_calendar_day(plant):
    sheet = ExtractedSheet(is_loss_sheet=True, sheet_date="2026-11-24", sheet_date_as_written=None, shift="C",
                           line="1", sku=None, unreadable=[],
                           stops=[stop("Wrapper", "23:50", "00:10", 20, "BD", "jam", "wrapper jam"),
                                  stop("Wrapper", "01:30", "01:34", 4, "MS", "jam", "wrapper jam")])
    ev = normalize_sheet(sheet, plant, photo_name="p", photo_sha="s", photo_time=None).events
    assert ev[0].start_at == "2026-11-24T23:50" and ev[0].duration_min == 20  # crosses midnight
    assert ev[1].start_at == "2026-11-25T01:30" and ev[1].shift_date == "2026-11-24"


def test_missing_date_uses_photo_time_and_night_shift_rolls_back(plant):
    sheet = ExtractedSheet(is_loss_sheet=True, sheet_date=None, sheet_date_as_written=None, shift="C",
                           line="1", sku=None, unreadable=["bottom edge cut off"],
                           stops=[stop("Wrapper", "02:00", "02:05", 5, "MS", "jam", "wrapper jam")])
    norm = normalize_sheet(sheet, plant, photo_name="p", photo_sha="s", photo_time=datetime(2026, 11, 25, 5, 40))
    assert norm.shift_date == "2026-11-24"
    assert any("photo date" in w for w in norm.warnings)
    assert any("bottom edge" in w for w in norm.warnings)


def test_shift_inferred_from_times_when_not_written(plant):
    sheet = ExtractedSheet(is_loss_sheet=True, sheet_date="2026-11-24", sheet_date_as_written=None, shift=None,
                           line=None, sku=None, unreadable=[],
                           stops=[stop("Wrapper", "07:15", "07:30", 15, None, "jam", "wrapper jam")])
    norm = normalize_sheet(sheet, plant, photo_name="p", photo_sha="s", photo_time=None)
    assert norm.shift == "A"
    assert any("inferred A" in w for w in norm.warnings)
    assert any("line not written" in w for w in norm.warnings)


def test_low_confidence_and_missing_values_are_flagged(plant):
    sheet = ExtractedSheet(is_loss_sheet=True, sheet_date="2026-11-24", sheet_date_as_written=None, shift="A",
                           line="1", sku=None, unreadable=[],
                           stops=[stop("Wrapper", "0?:15", None, None, None, None, None,
                                       confidence="low", note="time smudged")])
    e = normalize_sheet(sheet, plant, photo_name="p", photo_sha="s", photo_time=None).events[0]
    assert e.needs_review
    for part in ("low confidence read: time smudged", "not understood", "no duration", "no reason written"):
        assert part in e.review_note
    assert e.reason_group == "unspecified" and e.category == "unclassified"


def test_custom_plant_threshold_changes_rule():
    plant = PlantConfig.from_dict({"minor_stop_max_min": 5})
    assert classify("MS", "jam", 6, plant) == "breakdown"
