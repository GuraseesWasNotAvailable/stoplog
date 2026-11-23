"""Watch a folder for new photos (and optionally take them with a camera), read each one once."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .capture import Camera, CameraError, ChangeDetector
from .imaging import file_sha256, is_image_file
from .models import PhotoResult
from .pipeline import Pipeline, PhotoFailed

log = logging.getLogger("stoplog")

MAX_BACKOFF_S = 1800


@dataclass
class _Seen:
    size: int
    mtime_ns: int
    first_seen: float
    sha: str | None = None
    done: bool = False
    retries: int = 0
    next_try: float = 0.0


class DoneIndex:
    """Remembers which files were already handled, so a restart does not re-hash the whole inbox."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list] = {}
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def is_done(self, name: str, size: int, mtime_ns: int) -> bool:
        entry = self.data.get(name)
        return bool(entry) and entry[0] == size and entry[1] == mtime_ns

    def mark(self, name: str, size: int, mtime_ns: int, sha: str) -> None:
        self.data[name] = [size, mtime_ns, sha]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        os.replace(tmp, self.path)


def _ignored(path: Path) -> bool:
    n = path.name
    return (n.startswith(".") or n.startswith("~") or ".part." in n
            or n.endswith((".tmp", ".crdownload", ".partial", ".download")))


class FolderWatcher:
    def __init__(self, folder: Path, pipeline: Pipeline, *, settle_seconds: float = 5.0,
                 state_dir: Path = Path(".stoplog"), clock: Callable[[], float] = time.time):
        self.folder = Path(folder)
        self.pipeline = pipeline
        self.settle = settle_seconds
        self.clock = clock
        self.seen: dict[Path, _Seen] = {}
        self.index = DoneIndex(Path(state_dir) / "done_files.json")

    def candidates(self) -> list[Path]:
        if not self.folder.exists():
            return []
        return sorted(p for p in self.folder.iterdir() if p.is_file() and is_image_file(p) and not _ignored(p))

    def scan_once(self) -> list[PhotoResult]:
        now = self.clock()
        results: list[PhotoResult] = []
        for path in self.candidates():
            try:
                st = path.stat()
            except OSError:
                continue  # vanished between listing and stat
            s = self.seen.get(path)
            if s is None or (s.size, s.mtime_ns) != (st.st_size, st.st_mtime_ns):
                # New, or still being written / synced: wait until it holds still for a scan.
                self.seen[path] = _Seen(st.st_size, st.st_mtime_ns, now)
                if self.index.is_done(path.name, st.st_size, st.st_mtime_ns):
                    self.seen[path].done = True
                continue
            if s.done or now < s.next_try:
                continue
            if now - st.st_mtime_ns / 1e9 < self.settle:
                continue
            try:
                s.sha = s.sha or file_sha256(path)
                if self.pipeline.is_done(s.sha):
                    s.done = True
                    self.index.mark(path.name, s.size, s.mtime_ns, s.sha)
                    continue
                result = self.pipeline.process(path, s.sha)
                s.done = True
                self.index.mark(path.name, s.size, s.mtime_ns, s.sha)
                results.append(result)
                log.info("%s: %s, %d stop(s) read, %d added, %d already logged, %d to review%s",
                         path.name, result.status, result.stops_read, result.stops_added,
                         result.already_logged, result.needs_review,
                         (" | " + "; ".join(result.warnings)) if result.warnings else "")
            except PhotoFailed as exc:
                if exc.retryable:
                    self._backoff(s, now, f"{path.name}: {exc}")
                else:
                    s.done = True  # recorded as failed in the Photos tab; retried after a restart
                    log.error("%s: gave up: %s", path.name, exc)
            except Exception as exc:  # network / Sheets API trouble: keep the photo, try later
                self._backoff(s, now, f"{path.name}: {type(exc).__name__}: {exc}")
        return results

    def _backoff(self, s: _Seen, now: float, message: str) -> None:
        delay = min(MAX_BACKOFF_S, 30 * 2 ** min(s.retries, 6))
        s.retries += 1
        s.next_try = now + delay
        log.warning("%s (retry %d in %ds)", message, s.retries, delay)


class CaptureLoop:
    """Every `interval` seconds take a photo; keep it in the inbox only if the sheet changed."""

    def __init__(self, camera: Camera, inbox: Path, state_dir: Path, interval: float,
                 detector: ChangeDetector | None = None, clock: Callable[[], float] = time.time):
        self.camera, self.inbox, self.state_dir = camera, Path(inbox), Path(state_dir)
        self.interval = interval
        self.detector = detector or ChangeDetector()
        self.clock = clock
        self.next_at = 0.0

    def tick(self) -> Path | None:
        now = self.clock()
        if now < self.next_at:
            return None
        self.next_at = now + self.interval
        frame = self.state_dir / "last_capture.jpg"
        try:
            self.camera.capture(frame)
        except CameraError as exc:
            log.warning("capture failed: %s", exc)
            return None
        changed, score = self.detector.check(frame)
        if not changed:
            log.info("capture: no change on the sheet (score %.1f), not sent", score)
            return None
        self.inbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        dest = self.inbox / f"cam-{stamp}.jpg"
        n = 1
        while dest.exists():  # never overwrite a photo that has not been read yet
            dest = self.inbox / f"cam-{stamp}-{n}.jpg"
            n += 1
        shutil.copyfile(frame, dest.with_name(dest.stem + ".part.jpg"))
        os.replace(dest.with_name(dest.stem + ".part.jpg"), dest)
        self.detector.accept()
        log.info("capture: sheet changed (score %s), saved %s", "first" if score == float("inf") else f"{score:.1f}",
                 dest.name)
        return dest


def run_forever(watcher: FolderWatcher, poll_seconds: float, capture: CaptureLoop | None = None,
                sleep: Callable[[float], None] = time.sleep, max_loops: int | None = None) -> None:
    loops = 0
    dirty, retries, next_try = False, 0, 0.0
    log.info("watching %s every %ss%s", watcher.folder.resolve(), poll_seconds,
             f", capturing every {capture.interval:.0f}s with {capture.camera.backend}" if capture else "")
    while max_loops is None or loops < max_loops:
        loops += 1
        if capture is not None:
            capture.tick()
        results = watcher.scan_once()
        dirty = dirty or any(r.stops_added for r in results)
        now = watcher.clock()
        if dirty and now >= next_try:
            try:
                watcher.pipeline.rebuild_insights()
                dirty, retries = False, 0
                log.info("insights updated")
            except Exception as exc:  # offline: keep the flag and retry with backoff
                delay = min(MAX_BACKOFF_S, 30 * 2 ** min(retries, 6))
                retries += 1
                next_try = now + delay
                log.warning("insights update failed (retry in %ds): %s", delay, exc)
        sleep(poll_seconds)
