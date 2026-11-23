"""Data shapes: what the model returns for a photo, and the rows we write to the sheet.

The extraction models are sent to the LLM as a JSON schema (structured outputs), so every
field is required and has no default; "missing" is expressed as null.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ExtractedStop(BaseModel):
    machine: Optional[str] = Field(description="Machine or station as written, e.g. 'Wrapper', 'Stamper'.")
    start_time: Optional[str] = Field(description="When the stop began, 'HH:MM'. Apply AM/PM if written.")
    end_time: Optional[str] = Field(description="When the line restarted, 'HH:MM'. Apply AM/PM if written.")
    duration_min: Optional[float] = Field(description="Minutes as written for this row (total if a tally). Null if not written.")
    occurrences: int = Field(description="Number of identical stops this row records (tally marks, 'x4'); otherwise 1.")
    category_as_written: Optional[str] = Field(description="Stop type exactly as marked, e.g. 'BD', 'MS', 'C/O'.")
    reason_as_written: Optional[str] = Field(description="Reason verbatim, original language and script.")
    reason_english: Optional[str] = Field(description="The reason in plain English, one short sentence.")
    reason_group: Optional[str] = Field(description="2-4 word lowercase English cause label, reusing a known group when it fits.")
    action_taken: Optional[str] = Field(description="Action or remedy written, in English.")
    sku: Optional[str] = Field(description="Product/SKU if this row names one, else null.")
    recorded_by: Optional[str] = Field(description="Initials or name of who wrote the row, if any.")
    confidence: Literal["high", "medium", "low"]
    note: Optional[str] = Field(description="What is unclear about this row, if anything.")


class ExtractedSheet(BaseModel):
    is_loss_sheet: bool = Field(description="False if the photo is not a production stop/loss record.")
    sheet_date: Optional[str] = Field(description="Date on the sheet as YYYY-MM-DD, or null.")
    sheet_date_as_written: Optional[str] = Field(description="The date exactly as written, or null.")
    shift: Optional[str] = Field(description="Shift as written, e.g. 'B', 'II', 'Night'.")
    line: Optional[str] = Field(description="Line as written, e.g. 'Line 3', 'L-3'.")
    sku: Optional[str] = Field(description="Product/SKU running, if the sheet header names one.")
    stops: list[ExtractedStop]
    unreadable: list[str] = Field(description="Anything on the sheet that could not be read or is cut off.")


class Insight(BaseModel):
    finding: str = Field(description="What the data shows, with its numbers.")
    action: str = Field(description="One concrete next step that follows from the finding.")


class Narrative(BaseModel):
    insights: list[Insight]


LOG_COLUMNS = [
    "event_id",
    "shift_date",
    "shift",
    "line",
    "machine",
    "sku",
    "start",
    "end",
    "start_at",
    "duration_min",
    "category",
    "reason_group",
    "reason_detail",
    "reason_as_written",
    "category_as_written",
    "action_taken",
    "recorded_by",
    "confidence",
    "needs_review",
    "review_note",
    "photo",
    "photo_sha",
    "logged_at",
]

PHOTO_COLUMNS = [
    "photo_sha",
    "photo",
    "processed_at",
    "status",
    "sheet_date",
    "shift",
    "line",
    "stops_read",
    "stops_added",
    "already_logged",
    "needs_review",
    "model",
    "warnings",
]

# Photo statuses that count as "done": the photo is never sent to the model again.
DONE_STATUSES = {"logged", "not_a_sheet"}


@dataclass
class StopEvent:
    event_id: str
    shift_date: str
    shift: str
    line: str
    machine: str
    sku: str
    start: str
    end: str
    start_at: str
    duration_min: float | None
    category: str
    reason_group: str
    reason_detail: str
    reason_as_written: str
    category_as_written: str
    action_taken: str
    recorded_by: str
    confidence: str
    needs_review: bool
    review_note: str
    photo: str
    photo_sha: str
    logged_at: str

    def as_row(self) -> list:
        row = []
        for col in LOG_COLUMNS:
            value = getattr(self, col)
            if value is None:
                value = ""
            elif isinstance(value, bool):
                value = "yes" if value else ""
            elif isinstance(value, float):
                value = round(value, 1)
                if value == int(value):
                    value = int(value)
            row.append(value)
        return row


@dataclass
class PhotoResult:
    photo: str
    photo_sha: str
    status: str  # logged | not_a_sheet | already_processed | failed
    sheet_date: str = ""
    shift: str = ""
    line: str = ""
    stops_read: int = 0
    stops_added: int = 0
    already_logged: int = 0
    needs_review: int = 0
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    processed_at: str = ""

    def as_row(self) -> list:
        return [
            self.photo_sha,
            self.photo,
            self.processed_at,
            self.status,
            self.sheet_date,
            self.shift,
            self.line,
            self.stops_read,
            self.stops_added,
            self.already_logged,
            self.needs_review,
            self.model,
            "; ".join(self.warnings),
        ]
