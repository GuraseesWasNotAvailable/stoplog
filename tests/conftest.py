from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stoplog.config import PlantConfig, Settings  # noqa: E402
from stoplog.models import ExtractedSheet, ExtractedStop  # noqa: E402

SAMPLE_PHOTO = ROOT / "samples" / "line3-shiftB.jpg"


def stop(machine, start, end, minutes, code, reason, group, action="", by="RK", occurrences=1, row=None,
         confidence="high", note=None, english=None):
    return ExtractedStop(
        row_number=row, machine=machine, start_time=start, end_time=end, duration_min=minutes, occurrences=occurrences,
        category_as_written=code, reason_as_written=reason, reason_english=english or reason,
        reason_group=group, action_taken=action or None, sku=None, recorded_by=by,
        confidence=confidence, note=note,
    )


def sample_sheet() -> ExtractedSheet:
    """What a perfect read of samples/line3-shiftB.jpg returns (rows numbered 1-10, as printed)."""
    sheet = ExtractedSheet(
        is_loss_sheet=True, sheet_date="2026-11-24", sheet_date_as_written="24/11/2026", shift="B", page=None,
        line="Line 3", sku="Soap 100g",
        stops=[
            stop("Wrapper", "14:20", "14:26", 6, "MS", "film jam at sealing jaw", "film jam", "cleared film"),
            stop("Wrapper", "14:52", "15:30", 38, "BD", "sealing jaw heater not heating", "sealing jaw fault",
                 "replaced heater", "SM"),
            stop("Stamper", "15:41", "15:44", 3, "MS", "bar stuck in die", "stamper die jam", "removed bar"),
            stop("Wrapper", None, None, 10, "MS", "wrapper jam x5", "wrapper jam", "reset each time", occurrences=5),
            stop("Cartoner", "04:10", "04:22", 12, "BD", "carton feed jam", "carton feed jam", "adjusted guide"),
            stop("Line", "17:00", "17:30", 30, "PM", "tea break + cleaning", "cleaning"),
            stop("Wrapper", "18:05", "18:25", 15, "MAT", "film khatam, roll late from store", "no material",
                 "new roll fitted", english="Film ran out; the roll came late from the store."),
            stop("Conveyor", "19:10", "19:18", 8, "BD", "belt slipping", "conveyor belt fault", "tightened belt", "SM"),
            stop("Wrapper", "20:00", "20:45", 45, "CO", "SKU change 100g to 125g", "sku changeover"),
            stop("Stamper", "21:15", "21:21", 6, "MS", "die jam", "stamper die jam", "cleaned die"),
        ],
        unreadable=[],
    )
    for i, s in enumerate(sheet.stops, 1):
        s.row_number = i
    return sheet


class FakeLLM:
    """Returns queued results in order; an Exception in the queue is raised instead."""

    provider = "fake"

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def structured(self, **kwargs):
        self.calls.append(kwargs)
        if not self.results:
            raise AssertionError("FakeLLM called more times than expected")
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def plant() -> PlantConfig:
    return PlantConfig()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(openai_api_key="test-key", store="csv", csv_dir=tmp_path / "data",
                 watch_dir=tmp_path / "inbox", state_dir=tmp_path / "state")
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s
