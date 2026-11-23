"""Read one photo of a loss sheet into an ExtractedSheet with the configured LLM."""

from __future__ import annotations

from datetime import datetime

from .config import FORM_TYPE_CODES, PlantConfig, Settings
from .imaging import PreparedImage
from .llm import LLM
from .models import ExtractedSheet

SYSTEM_PROMPT = """\
You transcribe photos of handwritten production-line stop sheets (also called loss sheets, \
downtime logs or breakdown registers) from factory floors. Each row records one stoppage: \
the machine, when it stopped and restarted, how many minutes, a type code, the reason, the \
action taken and the initials of whoever wrote it.

The usual form is the "Line Stop Log". Header: Date, Shift (circled letter), Line, \
Product/SKU, Shift in-charge. Columns, left to right: No. | Machine | Stop time | Restart \
time | Minutes | Type | Reason (what happened) | Action taken | Initials. A legend of type \
codes is printed at the bottom. Other layouts and blank paper also occur; read whatever \
columns are there.

Rules:
- Transcribe what is written. Never invent a row, a time, a number or a reason. If a value \
is blank, missing or unreadable, return null, lower `confidence`, and say what is unclear \
in `note`.
- One entry in `stops` per filled row, top to bottom. Skip printed headers, the legend, \
totals, signatures, crossed-out rows and empty rows.
- Times: "HH:MM". If AM/PM is written, apply it and return 24-hour time. If not, return the \
digits as written (e.g. "2:40" -> "02:40"); shift timing is applied later.
- duration_min: the Minutes column for that row as a number ("1 hr 10" -> 70). Null if \
blank. Do not compute it from the times.
- occurrences: if one row records several identical stops (tally marks, "x4", "4 times"), \
the count; duration_min is then the row's total minutes. Otherwise 1.
- reason_as_written: verbatim, in the original language and script (Hindi, Tamil, \
Telugu, Hinglish and English all occur).
- reason_english: the reason in plain English, expanding shorthand ("b/d" = breakdown, \
"c/o" = changeover).
- reason_group: a 2-4 word lowercase English label for the failure mode, i.e. what went \
wrong ("sealing jaw heater fault", "film jam", "carton feed jam"), never just the machine. \
Reuse a known group only if it is the same failure; a heater, motor or sensor fault is not \
a jam. If none fits, write a new specific label.
- category_as_written: the Type code exactly as written (e.g. "BD", "MS"), or null.
- sheet_date: YYYY-MM-DD. Dates are written {date_format}. If the year is missing, use the \
photo's year. sheet_date_as_written: the date exactly as written.
- is_loss_sheet: false if the photo is not a production stop/loss record; then return no \
stops.
- unreadable: only writing you could not read, and parts of the sheet that are cut off, \
blurred, in shadow or covered. Not blank cells, totals, signatures or other observations; \
put doubts about a single row in that row's `note`.
"""


def build_user_prompt(plant: PlantConfig, photo_name: str, photo_time: datetime | None) -> str:
    lines = ["Plant context:"]
    lines.append("- Shifts: " + ", ".join(s.label() for s in plant.shifts))
    codes = ", ".join(f"{code} = {cat.replace('_', ' ')}" for code, cat, _ in FORM_TYPE_CODES)
    lines.append(f"- Type codes on the form: {codes}")
    if plant.lines:
        lines.append("- Lines: " + ", ".join(plant.lines))
    if plant.machines:
        lines.append("- Machines: " + ", ".join(plant.machines))
    if plant.reason_groups:
        lines.append("- Known reason groups: " + ", ".join(sorted(plant.reason_groups)))
    lines.append(f"- Photo file: {photo_name}")
    if photo_time:
        lines.append(f"- Photo taken: {photo_time:%Y-%m-%d %H:%M}")
    lines.append("")
    lines.append("Transcribe the stop sheet in this photo.")
    return "\n".join(lines)


class Extractor:
    """Wraps an LLM with the extraction prompt. Swap in a fake in tests."""

    def __init__(self, llm: LLM, settings: Settings):
        self.llm = llm
        self.settings = settings
        self.model = settings.extraction_model()

    def extract(self, image: PreparedImage, photo_name: str) -> ExtractedSheet:
        plant = self.settings.plant
        return self.llm.structured(
            model=self.model,
            system=SYSTEM_PROMPT.format(date_format=plant.date_format),
            text=build_user_prompt(plant, photo_name, image.taken_at),
            schema=ExtractedSheet,
            image_jpeg=image.jpeg,
            effort=self.settings.effort,
        )
