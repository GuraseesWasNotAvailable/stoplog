"""Where the running sheet lives. Google Sheets in production; CSV files for offline use and tests.

Both stores keep three tabs:
  Log       one row per stop (the running sheet)
  Photos    one row per photo processed (what was read, what was added, warnings)
  Insights  rebuilt from Log after every update
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import DONE_STATUSES, LOG_COLUMNS, PHOTO_COLUMNS

LOG_TAB, PHOTOS_TAB, INSIGHTS_TAB = "Log", "Photos", "Insights"


@dataclass
class InsightsGrid:
    """A rendered Insights tab: rows of cells plus which rows to style."""

    rows: list[list] = field(default_factory=list)
    title_rows: list[int] = field(default_factory=list)  # 0-based
    section_rows: list[int] = field(default_factory=list)
    header_rows: list[int] = field(default_factory=list)

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=1)


class Store(Protocol):
    def ensure_ready(self) -> None: ...
    def processed_photo_hashes(self) -> set[str]: ...
    def existing_event_ids(self) -> set[str]: ...
    def append_events(self, rows: list[list]) -> None: ...
    def append_photo(self, row: list) -> None: ...
    def read_events(self) -> list[dict[str, str]]: ...
    def write_insights(self, grid: InsightsGrid) -> None: ...
    def location(self) -> str: ...


def rows_to_dicts(values: list[list], columns: list[str]) -> list[dict[str, str]]:
    """Map sheet rows to dicts by the header row, tolerating extra/blank rows a human added."""
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    index = {name: header.index(name) for name in columns if name in header}
    out = []
    for raw in values[1:]:
        if not any(str(c).strip() for c in raw):
            continue
        out.append({name: (str(raw[i]).strip() if i < len(raw) else "") for name, i in index.items()})
    return out


class CsvStore:
    """Three CSV files in a folder. Same behaviour as the Google Sheet, no network."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.paths = {
            LOG_TAB: self.folder / "log.csv",
            PHOTOS_TAB: self.folder / "photos.csv",
            INSIGHTS_TAB: self.folder / "insights.csv",
        }

    def location(self) -> str:
        return str(self.folder.resolve())

    def ensure_ready(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        for tab, columns in ((LOG_TAB, LOG_COLUMNS), (PHOTOS_TAB, PHOTO_COLUMNS)):
            path = self.paths[tab]
            if not path.exists() or path.stat().st_size == 0:
                self._write(path, [columns])
            else:
                header = self._read(path)[0]
                if header != columns:
                    raise RuntimeError(f"{path} has unexpected columns {header}; expected {columns}")

    def _read(self, path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def _write(self, path: Path, rows: list[list]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        os.replace(tmp, path)

    def _append(self, path: Path, rows: list[list]) -> None:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)

    def processed_photo_hashes(self) -> set[str]:
        rows = rows_to_dicts(self._read(self.paths[PHOTOS_TAB]), PHOTO_COLUMNS)
        return {r["photo_sha"] for r in rows if r.get("status") in DONE_STATUSES}

    def existing_event_ids(self) -> set[str]:
        return {r["event_id"] for r in self.read_events() if r.get("event_id")}

    def append_events(self, rows: list[list]) -> None:
        if rows:
            self._append(self.paths[LOG_TAB], rows)

    def append_photo(self, row: list) -> None:
        self._append(self.paths[PHOTOS_TAB], [row])

    def read_events(self) -> list[dict[str, str]]:
        return rows_to_dicts(self._read(self.paths[LOG_TAB]), LOG_COLUMNS)

    def write_insights(self, grid: InsightsGrid) -> None:
        self._write(self.paths[INSIGHTS_TAB], grid.rows)
