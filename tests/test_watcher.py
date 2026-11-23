import os
import shutil
from datetime import datetime
from pathlib import Path

from conftest import SAMPLE_PHOTO, FakeLLM, sample_sheet
from PIL import Image, ImageDraw

from stoplog.capture import Camera, ChangeDetector
from stoplog.extract import Extractor
from stoplog.llm import LLMError
from stoplog.pipeline import Pipeline
from stoplog.store import CsvStore
from stoplog.watcher import CaptureLoop, FolderWatcher, run_forever


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def setup(settings, *results):
    store = CsvStore(settings.csv_dir)
    store.ensure_ready()
    llm = FakeLLM(*results)
    pipe = Pipeline(settings, store, Extractor(llm, settings), clock=lambda: datetime(2026, 11, 24, 22, 10))
    clock = Clock()
    settings.watch_dir.mkdir(parents=True, exist_ok=True)
    w = FolderWatcher(settings.watch_dir, pipe, settle_seconds=5, state_dir=settings.state_dir, clock=clock)
    return w, clock, llm


def drop(settings, name="sheet.jpg", mtime=None):
    dest = settings.watch_dir / name
    shutil.copyfile(SAMPLE_PHOTO, dest)
    if mtime is not None:
        os.utime(dest, (mtime, mtime))
    return dest


def test_waits_for_file_to_settle_then_reads_once(settings):
    w, clock, llm = setup(settings, sample_sheet())
    drop(settings, mtime=clock.t - 60)
    (settings.watch_dir / "upload.jpg.crdownload").write_bytes(b"x")
    (settings.watch_dir / "notes.txt").write_text("x")
    assert w.scan_once() == []  # first sight: wait one scan
    clock.t += 10
    results = w.scan_once()
    assert [r.stops_added for r in results] == [14]
    clock.t += 10
    assert w.scan_once() == [] and len(llm.calls) == 1


def test_restart_does_not_reread(settings):
    w, clock, _ = setup(settings, sample_sheet())
    drop(settings, mtime=clock.t - 60)
    w.scan_once()
    clock.t += 10
    w.scan_once()
    w2, clock2, llm2 = setup(settings)  # new process, same state dir
    clock2.t = clock.t + 10
    w2.scan_once()
    clock2.t += 10
    assert w2.scan_once() == [] and llm2.calls == []


def test_file_still_being_written_is_not_read(settings):
    w, clock, llm = setup(settings, sample_sheet())
    f = drop(settings, mtime=clock.t - 60)
    w.scan_once()
    with open(f, "ab") as fh:
        fh.write(b"\0" * 10)  # still syncing: size changed
    clock.t += 10
    assert w.scan_once() == [] and llm.calls == []


def test_network_error_backs_off_then_succeeds(settings):
    w, clock, llm = setup(settings, LLMError("connection reset", retryable=True), sample_sheet())
    drop(settings, mtime=clock.t - 60)
    w.scan_once()
    clock.t += 10
    assert w.scan_once() == []  # failed, backing off 30 s
    clock.t += 10
    assert w.scan_once() == [] and len(llm.calls) == 1  # still inside the backoff
    clock.t += 30
    assert [r.stops_added for r in w.scan_once()] == [14]


def test_run_forever_rebuilds_insights_after_new_rows(settings):
    w, clock, _ = setup(settings, sample_sheet())
    drop(settings, mtime=clock.t - 60)

    def sleep(s):
        clock.t += s

    run_forever(w, 10, sleep=sleep, max_loops=3)
    assert (settings.csv_dir / "insights.csv").exists()


class FakeCamera(Camera):
    def __init__(self, frames):
        self.frames = list(frames)
        self.backend = "fake"
        self.width, self.height, self.timeout, self.command = 0, 0, 0, None

    def capture(self, dest: Path) -> Path:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        self.frames.pop(0).save(dest)
        return dest


def sheet_frame(rows: int, jitter: int = 0) -> Image.Image:
    img = Image.new("RGB", (800, 600), (235 + jitter, 235 + jitter, 235 + jitter))
    d = ImageDraw.Draw(img)
    for i in range(rows):
        y = 60 + i * 40
        d.line([40, y, 760, y], fill=(30, 30, 30), width=2)
        d.text((60, y + 8), f"Wrapper 14:{10 + i:02d} jam", fill=(20, 30, 110))
    return img


def test_capture_keeps_only_changed_frames(settings):
    clock = Clock()
    cam = FakeCamera([sheet_frame(3), sheet_frame(3, jitter=2), sheet_frame(4)])
    loop = CaptureLoop(cam, settings.watch_dir, settings.state_dir, interval=600, clock=clock)
    assert loop.tick() is not None  # first frame always kept
    assert loop.tick() is None  # too soon
    clock.t += 600
    assert loop.tick() is None  # lighting flicker only
    clock.t += 600
    kept = loop.tick()  # a new row was written
    assert kept is not None and kept.parent == settings.watch_dir
    assert len(list(settings.watch_dir.glob("cam-*.jpg"))) == 2


def test_change_detector_scores(tmp_path):
    det = ChangeDetector()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    sheet_frame(3).save(a)
    sheet_frame(5).save(b)
    sa, sb = det.signature(a), det.signature(b)
    assert det.score(sa, sa) == 0
    assert det.score(sa, sb) >= det.threshold


def test_camera_commands():
    assert Camera(backend="usb").argv(Path("o.jpg"))[0] == "fswebcam"
    assert "-o" in Camera(backend="rpicam").argv(Path("o.jpg"))
    assert Camera(command="snap --file {out}").argv(Path("o.jpg")) == ["snap", "--file", "o.jpg"]
