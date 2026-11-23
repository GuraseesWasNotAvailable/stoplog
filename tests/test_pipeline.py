
from datetime import datetime

import pytest
from conftest import SAMPLE_PHOTO, FakeLLM, sample_sheet, stop
from PIL import Image

from stoplog.extract import Extractor
from stoplog.llm import LLMError
from stoplog.models import ExtractedSheet
from stoplog.pipeline import PhotoFailed, Pipeline
from stoplog.store import CsvStore


def make(settings, *results):
    llm = FakeLLM(*results)
    store = CsvStore(settings.csv_dir)
    store.ensure_ready()
    pipe = Pipeline(settings, store, Extractor(llm, settings), narrative_llm=None,
                    clock=lambda: datetime(2026, 11, 24, 22, 10))
    return pipe, store, llm


def test_photo_to_rows_and_insights(settings, tmp_path):
    pipe, store, llm = make(settings, sample_sheet())
    r = pipe.process(SAMPLE_PHOTO)
    assert (r.status, r.stops_read, r.stops_added, r.already_logged, r.needs_review) == ("logged", 14, 14, 0, 1)
    call = llm.calls[0]
    assert call["image_jpeg"][:2] == b"\xff\xd8" and call["effort"] == "high"
    assert "Line Stop Log" in call["system"] and "B (14:00-22:00)" in call["text"]
    assert len(store.read_events()) == 14

    facts = pipe.rebuild_insights()
    assert facts["totals"]["downtime_min"] == 148  # unplanned; 30 planned minutes reported apart
    assert (settings.csv_dir / "insights.csv").read_text(encoding="utf-8").startswith("Stop log insights")


def test_same_file_is_not_read_twice(settings):
    pipe, store, llm = make(settings, sample_sheet())
    pipe.process(SAMPLE_PHOTO)
    r = pipe.process(SAMPLE_PHOTO)
    assert r.status == "already_processed" and len(llm.calls) == 1
    # a fresh pipeline (restart) learns the same from the Photos tab
    pipe2 = Pipeline(settings, store, Extractor(FakeLLM(), settings))
    assert pipe2.process(SAMPLE_PHOTO).status == "already_processed"


def test_second_photo_of_same_sheet_only_adds_new_rows(settings, tmp_path):
    later = sample_sheet()
    later.stops.append(stop("Cutter", "21:40", "21:52", 12, "BD", "blade blunt", "cutter fault"))
    pipe, store, _ = make(settings, sample_sheet(), later)
    pipe.process(SAMPLE_PHOTO)
    second = tmp_path / "second.jpg"
    Image.open(SAMPLE_PHOTO).save(second, quality=70)  # different bytes, same sheet
    r = pipe.process(second)
    assert (r.stops_read, r.stops_added, r.already_logged) == (15, 1, 14)
    assert len(store.read_events()) == 15


def test_not_a_sheet(settings):
    junk = ExtractedSheet(is_loss_sheet=False, sheet_date=None, sheet_date_as_written=None, shift=None,
                          line=None, sku=None, stops=[], unreadable=[])
    pipe, store, _ = make(settings, junk)
    assert pipe.process(SAMPLE_PHOTO).status == "not_a_sheet"
    assert store.read_events() == []
    assert pipe.process(SAMPLE_PHOTO).status == "already_processed"


def test_retryable_error_leaves_photo_pending(settings):
    pipe, store, llm = make(settings, LLMError("rate limited", retryable=True), sample_sheet())
    with pytest.raises(PhotoFailed) as exc:
        pipe.process(SAMPLE_PHOTO)
    assert exc.value.retryable
    assert store.processed_photo_hashes() == set()
    assert pipe.process(SAMPLE_PHOTO).stops_added == 14


def test_permanent_error_is_recorded_but_not_marked_done(settings):
    pipe, store, _ = make(settings, LLMError("refused", retryable=False))
    with pytest.raises(PhotoFailed) as exc:
        pipe.process(SAMPLE_PHOTO)
    assert not exc.value.retryable
    photos = (settings.csv_dir / "photos.csv").read_text(encoding="utf-8")
    assert ",failed," in photos and "refused" in photos
    assert store.processed_photo_hashes() == set()  # a restart will try again


def test_unreadable_file(settings, tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not an image")
    pipe, _, llm = make(settings)
    with pytest.raises(PhotoFailed) as exc:
        pipe.process(bad)
    assert not exc.value.retryable and llm.calls == []


def test_csv_store_rejects_foreign_header(settings):
    settings.csv_dir.mkdir(parents=True)
    (settings.csv_dir / "log.csv").write_text("a,b,c\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        CsvStore(settings.csv_dir).ensure_ready()


def test_rows_survive_a_human_editing_the_sheet(settings):
    pipe, store, _ = make(settings, sample_sheet())
    pipe.process(SAMPLE_PHOTO)
    path = settings.csv_dir / "log.csv"
    text = path.read_text(encoding="utf-8").replace(",18:05,18:25,2026-11-24T18:05,20,", ",18:05,18:25,2026-11-24T18:05,15,")
    path.write_text(text + "\n,,,,\n", encoding="utf-8")  # a correction and a blank row
    facts = pipe.rebuild_insights()
    assert facts["totals"]["downtime_min"] == 143  # the corrected 15 min, not 20
