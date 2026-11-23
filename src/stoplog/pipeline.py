"""Photo in, rows out: hash -> read -> normalise -> dedupe -> append -> (later) insights."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Settings
from .extract import Extractor
from .imaging import ImageError, file_sha256, prepare_image
from .insights import build_insights
from .llm import LLMError
from .models import PhotoResult
from .normalize import normalize_sheet
from .store import Store

log = logging.getLogger("stoplog")


class PhotoFailed(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class Pipeline:
    def __init__(self, settings: Settings, store: Store, extractor: Extractor | None,
                 narrative_llm=None, clock: Callable[[], datetime] = datetime.now):
        self.settings = settings
        self.store = store
        self.extractor = extractor
        self.narrative_llm = narrative_llm
        self.clock = clock
        self._done_hashes: set[str] | None = None
        self._event_ids: set[str] | None = None

    # caches are loaded from the store once, then kept in step with our own writes
    def _load_caches(self) -> None:
        if self._done_hashes is None:
            self._done_hashes = self.store.processed_photo_hashes()
        if self._event_ids is None:
            self._event_ids = self.store.existing_event_ids()

    def is_done(self, sha: str) -> bool:
        self._load_caches()
        return sha in self._done_hashes  # type: ignore[operator]

    def process(self, path: str | Path, sha: str | None = None) -> PhotoResult:
        """Read one photo into the sheet. Raises PhotoFailed on errors worth retrying or reporting."""
        path = Path(path)
        self._load_caches()
        sha = sha or file_sha256(path)
        now = self.clock()
        result = PhotoResult(photo=path.name, photo_sha=sha, status="already_processed",
                             processed_at=now.isoformat(timespec="seconds"))
        if sha in self._done_hashes:  # type: ignore[operator]
            return result
        if self.extractor is None:
            raise PhotoFailed("no extractor configured (set an LLM API key)", retryable=False)

        try:
            image = prepare_image(path)
        except ImageError as exc:
            self._record_failure(result, str(exc))
            raise PhotoFailed(str(exc), retryable=False) from exc

        try:
            sheet = self.extractor.extract(image, path.name)
        except LLMError as exc:
            if not exc.retryable:
                self._record_failure(result, str(exc))
            raise PhotoFailed(str(exc), retryable=exc.retryable) from exc
        result.model = self.extractor.model

        if not sheet.is_loss_sheet:
            result.status = "not_a_sheet"
            result.warnings.append("photo does not look like a stop sheet")
            self.store.append_photo(result.as_row())
            self._done_hashes.add(sha)  # type: ignore[union-attr]
            return result

        norm = normalize_sheet(sheet, self.settings.plant, photo_name=path.name, photo_sha=sha,
                               photo_time=image.taken_at or datetime.fromtimestamp(path.stat().st_mtime),
                               now=now)
        new_rows, already = [], 0
        for ev in norm.events:
            if ev.event_id in self._event_ids:  # type: ignore[operator]
                already += 1
                continue
            self._event_ids.add(ev.event_id)  # type: ignore[union-attr]
            new_rows.append(ev)

        # Events first, then the photo row: if we crash in between, the photo is read again
        # next time and its events are skipped as already logged.
        self.store.append_events([ev.as_row() for ev in new_rows])
        result.status = "logged"
        result.sheet_date, result.shift, result.line = norm.shift_date, norm.shift, norm.line
        result.stops_read = len(norm.events)
        result.stops_added = len(new_rows)
        result.already_logged = already
        result.needs_review = sum(1 for ev in new_rows if ev.needs_review)
        result.warnings.extend(norm.warnings)
        self.store.append_photo(result.as_row())
        self._done_hashes.add(sha)  # type: ignore[union-attr]
        return result

    def _record_failure(self, result: PhotoResult, message: str) -> None:
        result.status = "failed"
        result.warnings.append(message)
        try:
            self.store.append_photo(result.as_row())
        except Exception:  # recording the failure must not hide the original error
            log.exception("could not record failure for %s", result.photo)

    def rebuild_insights(self) -> dict:
        model = self.settings.narrative_model() if self.narrative_llm is not None else ""
        grid, facts = build_insights(
            self.store.read_events(),
            self.settings.plant,
            llm=self.narrative_llm if self.settings.narrative else None,
            model=model,
            cache_dir=self.settings.state_dir,
            now=self.clock(),
            effort=self.settings.insights_effort,
        )
        self.store.write_insights(grid)
        return facts
