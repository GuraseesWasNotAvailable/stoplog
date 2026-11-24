import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from conftest import ROOT, SAMPLE_PHOTO, FakeLLM, sample_sheet, stop
from PIL import Image

from stoplog.extract import Extractor
from stoplog.models import LOG_COLUMNS, PHOTO_COLUMNS
from stoplog.pipeline import Pipeline
from stoplog.webapp import WebAppError, WebAppStore

GS = ROOT / "deploy" / "google-apps-script" / "stoplog-bridge.gs"
HARNESS = Path(__file__).with_name("gas_harness.js")
URL = "https://script.google.com/macros/s/TEST/exec"


def test_script_columns_match_python():
    """The Apps Script carries its own copy of the headers; it must not drift."""
    text = GS.read_text(encoding="utf-8")
    log = re.findall(r'"([^"]+)"', re.search(r"Log: \[(.*?)\]", text, re.S).group(1))
    photos = re.findall(r'"([^"]+)"', re.search(r"Photos: \[(.*?)\]", text, re.S).group(1))
    assert log == LOG_COLUMNS and photos == PHOTO_COLUMNS


def test_url_must_be_apps_script():
    with pytest.raises(WebAppError):
        WebAppStore("https://example.com/hook")
    with pytest.raises(WebAppError):
        WebAppStore("")


def test_bridge_error_surfaces():
    store = WebAppStore(URL, transport=lambda url, p: {"ok": False, "error": "Tab 'Log' already has a different header row"})
    with pytest.raises(WebAppError, match="different header"):
        store.ensure_ready()


@pytest.fixture
def bridge():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    proc = subprocess.Popen(["node", str(HARNESS), str(GS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, encoding="utf-8")

    def transport(url, payload):
        proc.stdin.write(json.dumps(payload, default=str) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    yield transport
    proc.stdin.close()
    proc.wait(timeout=10)


def test_pipeline_through_the_real_apps_script(settings, tmp_path, bridge):
    store = WebAppStore(URL, transport=bridge)
    store.ensure_ready()
    store.ensure_ready()  # idempotent
    assert store.location().endswith("/TEST/edit")

    later = sample_sheet()
    later.stops.append(stop("Cutter", "21:40", "21:52", 12, "BD", "=HYPERLINK(\"x\")", "cutter fault", row=11))
    pipe = Pipeline(settings, store, Extractor(FakeLLM(sample_sheet(), later), settings),
                    clock=lambda: datetime(2026, 11, 24, 22, 10))
    assert pipe.process(SAMPLE_PHOTO).stops_added == 14

    second = tmp_path / "second.jpg"
    Image.open(SAMPLE_PHOTO).save(second, quality=70)
    r = pipe.process(second)
    assert (r.stops_added, r.already_logged) == (1, 14)

    # text stayed text: no date conversion, and the formula-looking reason is not a formula
    kinds = dict(bridge(URL, {"action": "__dump", "tab": "Log"})["result"])
    assert kinds["2,2"] == "string"  # shift_date
    assert "formula" not in kinds.values()
    assert kinds["2,10"] == "number"  # duration_min

    # a person typed a real date into the sheet: it comes back as text stoplog parses
    bridge(URL, {"action": "__set_date", "tab": "Log", "row": 2, "col": 2, "iso": "2026-11-24T00:00:00"})
    events = store.read_events()
    assert len(events) == 15 and events[0]["shift_date"] == "2026-11-24"

    facts = pipe.rebuild_insights()
    assert facts["totals"]["downtime_min"] == 160  # 148 + the 12-min cutter stop

    # a restart reads the done photos back from the Photos tab
    fresh = Pipeline(settings, WebAppStore(URL, transport=bridge), Extractor(FakeLLM(), settings))
    assert fresh.process(SAMPLE_PHOTO).status == "already_processed"


def test_foreign_header_is_refused_and_bad_actions_fail(bridge):
    bridge(URL, {"action": "__set", "tab": "Log", "row": 1, "col": 1, "value": "Date"})  # someone's own tab
    with pytest.raises(WebAppError, match="different header row"):
        WebAppStore(URL, transport=bridge).ensure_ready()
    assert "unknown action" in bridge(URL, {"action": "nope"})["error"]
    assert "unknown tab" in bridge(URL, {"action": "append", "tab": "Secrets", "rows": [["x"]]})["error"]
