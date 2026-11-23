"""The running sheet in Google Sheets (via gspread).

Auth: point GOOGLE_CREDENTIALS_FILE at either
  * a service-account key JSON (best for an unattended Raspberry Pi; share the sheet with the
    service account's email as Editor), or
  * an OAuth "Desktop app" client JSON (opens a browser once, then reuses the saved token).
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import DONE_STATUSES, LOG_COLUMNS, PHOTO_COLUMNS
from .store import INSIGHTS_TAB, LOG_TAB, PHOTOS_TAB, InsightsGrid, rows_to_dicts

_TAB_SPECS = {LOG_TAB: LOG_COLUMNS, PHOTOS_TAB: PHOTO_COLUMNS}


class SheetSetupError(RuntimeError):
    pass


def _col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def open_client(credentials_file: Path, token_dir: Path):
    import gspread
    from gspread.http_client import BackOffHTTPClient  # retries 429/5xx with backoff

    if not credentials_file or not Path(credentials_file).exists():
        raise SheetSetupError(
            f"Google credentials file not found: {credentials_file!s}. Set GOOGLE_CREDENTIALS_FILE."
        )
    with open(credentials_file, encoding="utf-8") as fh:
        info = json.load(fh)
    if info.get("type") == "service_account":
        return gspread.service_account(filename=str(credentials_file), http_client=BackOffHTTPClient)
    if "installed" in info or "web" in info:
        token_dir.mkdir(parents=True, exist_ok=True)
        return gspread.oauth(
            credentials_filename=str(credentials_file),
            authorized_user_filename=str(token_dir / "google_token.json"),
            http_client=BackOffHTTPClient,
        )
    raise SheetSetupError(
        f"{credentials_file} is neither a service-account key nor an OAuth client file."
    )


class GoogleSheetStore:
    def __init__(self, sheet_id: str, credentials_file: Path | None = None, token_dir: Path = Path(".stoplog"),
                 client=None):
        if not sheet_id:
            raise SheetSetupError("STOPLOG_SHEET_ID is not set (the id or URL of your Google Sheet).")
        self.sheet_id = sheet_id
        self._client = client
        self._credentials_file = credentials_file
        self._token_dir = token_dir
        self._spreadsheet = None
        self._ws: dict[str, object] = {}

    # ------------------------------------------------------------------ setup

    @property
    def spreadsheet(self):
        if self._spreadsheet is None:
            import gspread

            if self._client is None:
                self._client = open_client(self._credentials_file, self._token_dir)
            try:
                self._spreadsheet = self._client.open_by_key(self.sheet_id)
            except gspread.exceptions.SpreadsheetNotFound as exc:
                who = service_account_email(self._credentials_file)
                hint = f" Share it with {who} as Editor." if who else ""
                raise SheetSetupError(f"Cannot open sheet {self.sheet_id}.{hint}") from exc
            except gspread.exceptions.APIError as exc:
                if getattr(exc, "code", None) == 403:
                    who = service_account_email(self._credentials_file)
                    hint = f" Share it with {who} as Editor." if who else ""
                    raise SheetSetupError(f"No access to sheet {self.sheet_id}.{hint}") from exc
                raise
        return self._spreadsheet

    def location(self) -> str:
        return getattr(self.spreadsheet, "url", f"https://docs.google.com/spreadsheets/d/{self.sheet_id}")

    def _worksheet(self, title: str):
        if title not in self._ws:
            import gspread

            try:
                self._ws[title] = self.spreadsheet.worksheet(title)
            except gspread.exceptions.WorksheetNotFound:
                cols = len(_TAB_SPECS.get(title, [])) or 12
                self._ws[title] = self.spreadsheet.add_worksheet(title=title, rows=200, cols=cols)
        return self._ws[title]

    def ensure_ready(self) -> None:
        for title, columns in _TAB_SPECS.items():
            ws = self._worksheet(title)
            header = [str(c).strip() for c in ws.row_values(1)]
            if not any(header):
                ws.update(values=[columns], range_name="A1", value_input_option="RAW")
                last = _col_letter(len(columns))
                ws.format(f"A1:{last}1", {"textFormat": {"bold": True},
                                          "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93}})
                ws.freeze(rows=1)
            elif header[: len(columns)] != columns:
                raise SheetSetupError(
                    f"Tab '{title}' already has a different header row. Rename that tab or use an "
                    f"empty sheet; stoplog will not overwrite it. Found: {header[:6]}..."
                )
        self._worksheet(INSIGHTS_TAB)

    # ------------------------------------------------------------------ reads

    def _values(self, title: str) -> list[list]:
        return self._worksheet(title).get_all_values(value_render_option="UNFORMATTED_VALUE")

    def processed_photo_hashes(self) -> set[str]:
        rows = rows_to_dicts(self._values(PHOTOS_TAB), PHOTO_COLUMNS)
        return {r["photo_sha"] for r in rows if r.get("status") in DONE_STATUSES}

    def existing_event_ids(self) -> set[str]:
        return {r["event_id"] for r in self.read_events() if r.get("event_id")}

    def read_events(self) -> list[dict[str, str]]:
        return rows_to_dicts(self._values(LOG_TAB), LOG_COLUMNS)

    # ------------------------------------------------------------------ writes

    def append_events(self, rows: list[list]) -> None:
        if rows:
            # RAW: values are stored exactly as given (no date/formula parsing, no injection).
            self._worksheet(LOG_TAB).append_rows(rows, value_input_option="RAW", table_range="A1")

    def append_photo(self, row: list) -> None:
        self._worksheet(PHOTOS_TAB).append_rows([row], value_input_option="RAW", table_range="A1")

    def write_insights(self, grid: InsightsGrid) -> None:
        ws = self._worksheet(INSIGHTS_TAB)
        width = grid.width
        rows = [list(r) + [""] * (width - len(r)) for r in grid.rows]
        need_rows = max(len(rows) + 5, 50)
        ws.clear()
        ws.resize(rows=need_rows, cols=max(width, 8))
        ws.update(values=rows, range_name="A1", value_input_option="RAW")

        last = _col_letter(max(width, 8))
        formats = [{"range": f"A1:{last}{need_rows}",
                    "format": {"textFormat": {"bold": False, "fontSize": 10},
                               "backgroundColor": {"red": 1, "green": 1, "blue": 1}}}]
        for r in grid.title_rows:
            formats.append({"range": f"A{r + 1}", "format": {"textFormat": {"bold": True, "fontSize": 14}}})
        for r in grid.section_rows:
            formats.append({"range": f"A{r + 1}:{last}{r + 1}",
                            "format": {"textFormat": {"bold": True, "fontSize": 11}}})
        for r in grid.header_rows:
            formats.append({"range": f"A{r + 1}:{last}{r + 1}",
                            "format": {"textFormat": {"bold": True},
                                       "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93}}})
        ws.batch_format(formats)


def service_account_email(credentials_file: Path | None) -> str | None:
    try:
        with open(credentials_file, encoding="utf-8") as fh:  # type: ignore[arg-type]
            info = json.load(fh)
        return info.get("client_email") if info.get("type") == "service_account" else None
    except Exception:
        return None
