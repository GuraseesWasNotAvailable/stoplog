"""The running sheet through a Google Apps Script web app bound to it.

No Google Cloud project, service account or key file: the script runs as the sheet's owner.
Paste deploy/google-apps-script/stoplog-bridge.gs into the sheet (Extensions > Apps Script),
deploy it as a web app, and set STOPLOG_WEBAPP_URL. The URL is the only credential, so it is
read from the environment like a key, never from the YAML.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from .models import DONE_STATUSES, LOG_COLUMNS, PHOTO_COLUMNS
from .store import LOG_TAB, PHOTOS_TAB, InsightsGrid, rows_to_dicts


class WebAppError(RuntimeError):
    """The bridge answered with an error (bad setup, foreign header, ...): not worth retrying."""


def _post_json(url: str, payload: dict, timeout: float = 90.0) -> dict:
    body = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "stoplog"})
    # Apps Script answers a POST with a redirect to the result; urllib follows it with a GET.
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except ValueError:
        hint = "sign-in page" if "<html" in raw[:500].lower() else "unexpected reply"
        raise WebAppError(
            f"The web app returned a {hint}, not JSON. Check the deployment: Execute as 'Me', "
            f"Who has access 'Anyone', and that STOPLOG_WEBAPP_URL is the /exec URL."
        ) from None


class WebAppStore:
    def __init__(self, url: str, transport: Callable[[str, dict], dict] | None = None):
        if not url or not url.startswith("https://script.google.com/"):
            raise WebAppError("STOPLOG_WEBAPP_URL is not set to the Apps Script web app URL "
                              "(https://script.google.com/macros/s/.../exec).")
        self.url = url
        self._transport = transport or _post_json
        self._sheet_url: str | None = None

    def _call(self, action: str, **payload):
        reply = self._transport(self.url, {"action": action, **payload})
        if not reply.get("ok"):
            raise WebAppError(f"stoplog bridge: {reply.get('error', 'unknown error')}")
        return reply.get("result")

    def location(self) -> str:
        if self._sheet_url is None:
            try:
                self._sheet_url = self._call("ping").get("url") or "(sheet)"
            except (WebAppError, urllib.error.URLError, OSError):
                return "(sheet behind the web app)"
        return self._sheet_url

    def ensure_ready(self) -> None:
        self._sheet_url = (self._call("ensure_ready") or {}).get("url")

    def _read(self, tab: str) -> list[list]:
        return self._call("read", tab=tab) or []

    def processed_photo_hashes(self) -> set[str]:
        rows = rows_to_dicts(self._read(PHOTOS_TAB), PHOTO_COLUMNS)
        return {r["photo_sha"] for r in rows if r.get("status") in DONE_STATUSES}

    def existing_event_ids(self) -> set[str]:
        return {r["event_id"] for r in self.read_events() if r.get("event_id")}

    def read_events(self) -> list[dict[str, str]]:
        return rows_to_dicts(self._read(LOG_TAB), LOG_COLUMNS)

    def append_events(self, rows: list[list]) -> None:
        if rows:
            self._call("append", tab=LOG_TAB, rows=rows)

    def append_photo(self, row: list) -> None:
        self._call("append", tab=PHOTOS_TAB, rows=[row])

    def write_insights(self, grid: InsightsGrid) -> None:
        self._call("write_insights", rows=grid.rows, title_rows=grid.title_rows,
                   section_rows=grid.section_rows, header_rows=grid.header_rows)
