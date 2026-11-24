"""Command line: stoplog init | watch | process | insights | extract | print-form | capture-test."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Settings

log = logging.getLogger("stoplog")


def make_store(settings: Settings):
    if settings.store == "csv":
        from .store import CsvStore

        return CsvStore(settings.csv_dir)
    if settings.store == "gsheet":
        from .gsheet import GoogleSheetStore

        return GoogleSheetStore(settings.sheet_id or "", settings.google_credentials, settings.state_dir)
    if settings.store == "webapp":
        from .webapp import WebAppStore

        return WebAppStore(settings.webapp_url or "")
    raise SystemExit(f"STOPLOG_STORE must be 'webapp', 'gsheet' or 'csv', got {settings.store!r}")


def make_pipeline(settings: Settings, store=None):
    from .extract import Extractor
    from .llm import make_llm
    from .pipeline import Pipeline

    llm = make_llm(settings)
    return Pipeline(settings, store or make_store(settings), Extractor(llm, settings), narrative_llm=llm)


def _settings(args) -> Settings:
    s = Settings.load(args.config)
    if args.store:
        s.store = args.store
    if args.csv_dir:
        s.csv_dir = Path(args.csv_dir)
    if getattr(args, "no_narrative", False):
        s.narrative = False
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


# ---------------------------------------------------------------- commands


def cmd_init(args) -> int:
    s = _settings(args)
    ok = True
    try:
        provider = s.resolved_provider()
        print(f"LLM: {provider}, model {s.extraction_model()}")
    except RuntimeError as exc:
        print(f"LLM: NOT READY. {exc}")
        ok = False
    print("Shifts: " + ", ".join(sh.label() for sh in s.plant.shifts)
          + f" | minor stop under {s.plant.minor_stop_max_min:g} min | dates {s.plant.date_format}")
    try:
        store = make_store(s)
        store.ensure_ready()
        print(f"Sheet ready: {store.location()}")
    except Exception as exc:
        print(f"Sheet: NOT READY. {exc}")
        ok = False
    if s.store == "gsheet" and s.google_credentials:
        from .gsheet import service_account_email

        who = service_account_email(s.google_credentials)
        if who:
            print(f"Service account: {who} (share the sheet with it as Editor)")
    if datetime.now().year < 2024:
        print("WARNING: the system clock looks wrong. On a Raspberry Pi, check network time (timedatectl).")
        ok = False
    print("Inbox folder:", s.watch_dir.resolve())
    return 0 if ok else 1


def cmd_process(args) -> int:
    from .pipeline import PhotoFailed

    s = _settings(args)
    pipe = make_pipeline(s)
    pipe.store.ensure_ready()
    added, failed = 0, 0
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(f"{name}: not a file")
            failed += 1
            continue
        try:
            r = pipe.process(path)
        except PhotoFailed as exc:
            print(f"{path.name}: FAILED: {exc}")
            failed += 1
            continue
        added += r.stops_added
        print(f"{path.name}: {r.status}, {r.stops_read} read, {r.stops_added} added, "
              f"{r.already_logged} already logged, {r.needs_review} to review")
        for w in r.warnings:
            print(f"  - {w}")
    if added and not args.no_insights:
        pipe.rebuild_insights()
        print("Insights updated:", pipe.store.location())
    return 1 if failed else 0


def cmd_rebuild(args) -> int:
    """Fill an emptied sheet again: tabs, every photo in the folder, insights."""
    from .imaging import is_image_file
    from .pipeline import PhotoFailed
    from .watcher import _ignored

    s = _settings(args)
    folder = Path(args.folder) if args.folder else s.watch_dir
    pipe = make_pipeline(s)
    pipe.store.ensure_ready()
    print(f"Tabs ready: {pipe.store.location()}")
    photos = sorted((p for p in folder.iterdir() if p.is_file() and is_image_file(p) and not _ignored(p)),
                    key=lambda p: p.stat().st_mtime)
    added = failed = 0
    for path in photos:
        try:
            r = pipe.process(path, fresh=args.fresh)
        except PhotoFailed as exc:
            print(f"{path.name}: FAILED: {exc}")
            failed += 1
            continue
        added += r.stops_added
        source = "saved read" if any("saved read" in w for w in r.warnings) else "new read"
        print(f"{path.name}: {r.status} ({source}), {r.stops_added} added, {r.already_logged} already there")
    facts = pipe.rebuild_insights()
    t = facts["totals"]
    print(f"Done: {len(photos)} photo(s), {added} stop(s) added, insights rebuilt "
          f"({t['stops']} unplanned stops, {t['downtime_min']} min).")
    return 1 if failed else 0


def cmd_watch(args) -> int:
    from .capture import Camera, ChangeDetector
    from .watcher import CaptureLoop, FolderWatcher, run_forever

    s = _settings(args)
    folder = Path(args.folder) if args.folder else s.watch_dir
    folder.mkdir(parents=True, exist_ok=True)
    pipe = make_pipeline(s)
    pipe.store.ensure_ready()
    capture = None
    if args.capture:
        camera = Camera(backend=args.camera, command=args.capture_command)
        capture = CaptureLoop(camera, folder, s.state_dir, interval=args.every,
                              detector=ChangeDetector(threshold=args.change_threshold))
    watcher = FolderWatcher(folder, pipe, settle_seconds=s.settle_seconds, state_dir=s.state_dir)
    try:
        run_forever(watcher, s.poll_seconds, capture)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_insights(args) -> int:
    s = _settings(args)
    store = make_store(s)
    store.ensure_ready()
    llm = None
    if s.narrative:
        try:
            from .llm import make_llm

            llm = make_llm(s)
        except RuntimeError as exc:
            print(f"(no written findings: {exc})")
    from .pipeline import Pipeline

    facts = Pipeline(s, store, None, narrative_llm=llm).rebuild_insights()
    t = facts["totals"]
    print(f"{t['stops']} stops, {t['downtime_min']} min down "
          f"({t['downtime_pct_of_logged_time']}% of logged shift time). Written to {store.location()}")
    return 0


def cmd_extract(args) -> int:
    """Read one photo and print what would be logged. Writes nothing."""
    from .extract import Extractor
    from .imaging import file_sha256, prepare_image
    from .llm import make_llm
    from .normalize import normalize_sheet

    s = _settings(args)
    path = Path(args.file)
    image = prepare_image(path)
    sheet = Extractor(make_llm(s), s).extract(image, path.name)
    norm = normalize_sheet(sheet, s.plant, photo_name=path.name, photo_sha=file_sha256(path),
                           photo_time=image.taken_at or datetime.fromtimestamp(path.stat().st_mtime))
    out = {"read": sheet.model_dump(), "rows": [dict(zip(_cols(), e.as_row())) for e in norm.events],
           "warnings": norm.warnings}
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(text)
    return 0


def _cols():
    from .models import LOG_COLUMNS

    return LOG_COLUMNS


def cmd_print_form(args) -> int:
    from .form import save_form

    s = _settings(args)
    print(save_form(args.out, s.plant, dpi=args.dpi))
    return 0


def cmd_capture_test(args) -> int:
    from .capture import Camera

    cam = Camera(backend=args.camera, command=args.capture_command)
    out = cam.capture(Path(args.out))
    print(f"{cam.backend}: saved {out}. Check the whole sheet is in frame, sharp, and evenly lit.")
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stoplog", description="Photos of line stop sheets -> a live Google Sheet.")
    p.add_argument("--version", action="version", version=f"stoplog {__version__}")
    p.add_argument("--config", help="plant config YAML (default: stoplog.yaml if present)")
    p.add_argument("--store", choices=["webapp", "gsheet", "csv"], help="override STOPLOG_STORE")
    p.add_argument("--csv-dir", help="folder for the CSV store")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", help="also write the log here (rotated at 1 MB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="check keys and the sheet, create the tabs").set_defaults(fn=cmd_init)

    sp = sub.add_parser("process", help="read photos now")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--no-insights", action="store_true", help="skip rebuilding the Insights tab")
    sp.add_argument("--no-narrative", action="store_true", help="tables only, no AI-written findings")
    sp.set_defaults(fn=cmd_process)

    def camera_args(sp):
        sp.add_argument("--camera", default="auto", choices=["auto", "rpicam", "usb"])
        sp.add_argument("--capture-command", help="custom capture command with {out}, e.g. 'my-cam {out}'")

    sp = sub.add_parser("watch", help="watch the inbox folder (and optionally take photos) forever")
    sp.add_argument("--folder", help="inbox folder (default STOPLOG_WATCH_DIR or ./inbox)")
    sp.add_argument("--capture", action="store_true", help="take photos with the Pi camera / webcam")
    sp.add_argument("--every", type=float, default=600, help="seconds between photos (default 600)")
    sp.add_argument("--change-threshold", type=float, default=4.0,
                    help="how different a photo must be to be read (default 4; lower = more sensitive)")
    sp.add_argument("--no-narrative", action="store_true")
    camera_args(sp)
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("rebuild", help="refill an emptied sheet: tabs, every photo in the folder, insights")
    sp.add_argument("--folder", help="photo folder (default STOPLOG_WATCH_DIR)")
    sp.add_argument("--fresh", action="store_true", help="read every photo again instead of using saved reads")
    sp.add_argument("--no-narrative", action="store_true")
    sp.set_defaults(fn=cmd_rebuild)

    sp = sub.add_parser("insights", help="rebuild the Insights tab from the Log tab")
    sp.add_argument("--no-narrative", action="store_true")
    sp.set_defaults(fn=cmd_insights)

    sp = sub.add_parser("extract", help="read one photo and print the result; writes nothing")
    sp.add_argument("file")
    sp.add_argument("--out", help="write the JSON here instead of printing")
    sp.set_defaults(fn=cmd_extract)

    sp = sub.add_parser("print-form", help="write the printable Line Stop Log (PDF or PNG)")
    sp.add_argument("out", nargs="?", default="line-stop-log.pdf")
    sp.add_argument("--dpi", type=int, default=200)
    sp.set_defaults(fn=cmd_print_form)

    sp = sub.add_parser("capture-test", help="take one photo to check camera framing")
    sp.add_argument("out", nargs="?", default="capture-test.jpg")
    camera_args(sp)
    sp.set_defaults(fn=cmd_capture_test)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: list[logging.Handler] = []
    if args.log_file:  # for running without a console (pythonw, Task Scheduler, systemd)
        from logging.handlers import RotatingFileHandler

        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    # force=True: an import may already have configured the root logger, which would make
    # basicConfig a silent no-op and leave the log file empty under pythonw.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers or None,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        force=True)
    for noisy in ("httpx", "httpx2", "openai", "anthropic", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return args.fn(args) or 0
    except RuntimeError as exc:  # configuration problems: say what to fix, no traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
