import gspread
import pytest

from stoplog.config import PlantConfig, Settings, _sheet_id
from stoplog.form import COLUMNS, ROWS, render_form, save_form
from stoplog.gsheet import GoogleSheetStore, SheetSetupError
from stoplog.models import LOG_COLUMNS, PHOTO_COLUMNS
from stoplog.store import InsightsGrid


class FakeWorksheet:
    def __init__(self, title):
        self.title, self.cells, self.calls = title, [], []

    def row_values(self, n):
        return list(self.cells[n - 1]) if len(self.cells) >= n else []

    def update(self, values, range_name=None, value_input_option=None, **kw):
        self.calls.append(("update", range_name, value_input_option))
        assert range_name == "A1"
        for i, row in enumerate(values):
            while len(self.cells) <= i:
                self.cells.append([])
            self.cells[i] = list(row)

    def append_rows(self, rows, value_input_option=None, table_range=None, **kw):
        self.calls.append(("append", value_input_option, table_range))
        self.cells.extend(list(r) for r in rows)

    def get_all_values(self, value_render_option=None, **kw):
        return [list(r) for r in self.cells]

    def clear(self):
        self.calls.append(("clear",))
        self.cells = []

    def resize(self, rows=None, cols=None):
        self.calls.append(("resize", rows, cols))

    def format(self, *a, **k):
        self.calls.append(("format",))

    def freeze(self, rows=None, cols=None):
        self.calls.append(("freeze", rows))

    def batch_format(self, formats):
        self.calls.append(("batch_format", len(formats)))


class FakeSpreadsheet:
    url = "https://docs.google.com/spreadsheets/d/abc"

    def __init__(self):
        self.tabs = {}

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWorksheet(title)
        return self.tabs[title]


class FakeClient:
    def __init__(self):
        self.sheet = FakeSpreadsheet()

    def open_by_key(self, key):
        return self.sheet


def test_sheet_setup_append_read_and_insights():
    client = FakeClient()
    store = GoogleSheetStore("abc", client=client)
    store.ensure_ready()
    tabs = client.sheet.tabs
    assert set(tabs) == {"Log", "Photos", "Insights"}
    assert tabs["Log"].cells[0] == LOG_COLUMNS and tabs["Photos"].cells[0] == PHOTO_COLUMNS
    store.ensure_ready()  # idempotent

    row = ["id1", "2026-11-24", "B"] + [""] * (len(LOG_COLUMNS) - 3)
    store.append_events([row])
    assert ("append", "RAW", "A1") in tabs["Log"].calls
    assert store.existing_event_ids() == {"id1"}
    store.append_photo(["sha1", "p.jpg", "t", "logged"] + [""] * (len(PHOTO_COLUMNS) - 4))
    assert store.processed_photo_hashes() == {"sha1"}

    grid = InsightsGrid(rows=[["Stop log insights"], ["a", 1]], title_rows=[0], header_rows=[1])
    store.write_insights(grid)
    kinds = [c[0] for c in tabs["Insights"].calls]
    assert kinds == ["clear", "resize", "update", "batch_format"]
    assert tabs["Insights"].cells[1][:2] == ["a", 1]
    assert store.location() == FakeSpreadsheet.url


def test_sheet_refuses_to_overwrite_someone_elses_tab():
    client = FakeClient()
    client.sheet.add_worksheet("Log", 10, 3).cells = [["Date", "Notes"]]
    with pytest.raises(SheetSetupError):
        GoogleSheetStore("abc", client=client).ensure_ready()


def test_missing_sheet_id():
    with pytest.raises(SheetSetupError):
        GoogleSheetStore("", client=FakeClient())


def test_sheet_id_from_url():
    assert _sheet_id("https://docs.google.com/spreadsheets/d/1AbC_x-9/edit#gid=0") == "1AbC_x-9"
    assert _sheet_id(" 1AbC ") == "1AbC"


def test_form_layout_and_files(tmp_path):
    img, lay = render_form(PlantConfig())
    assert img.size == (2339, 1654)  # A4 landscape at 200 dpi
    assert len(lay.cells) == ROWS and set(lay.cells[0]) == {k for k, _, _ in COLUMNS}
    assert set(lay.shift_marks) == {"A", "B", "C"}
    assert save_form(tmp_path / "f.pdf").read_bytes()[:4] == b"%PDF"
    assert save_form(tmp_path / "f.png").exists()


def test_settings_load_from_env_and_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "plant.yaml"
    cfg.write_text("shifts:\n  - {name: Day, start: '08:00', end: '20:00'}\n  - {name: Night, start: '20:00', end: '08:00'}\n"
                   "minor_stop_max_min: 5\nreason_groups:\n  wrapper jam: [wrap jam]\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("STOPLOG_SHEET_ID", "https://docs.google.com/spreadsheets/d/XYZ/edit")
    s = Settings.load(cfg, env_file=None)
    assert s.resolved_provider() == "anthropic" and s.sheet_id == "XYZ"
    assert [sh.name for sh in s.plant.shifts] == ["Day", "Night"] and s.plant.shifts[1].minutes == 720
    assert s.plant.minor_stop_max_min == 5 and list(s.plant.reason_groups) == ["wrapper jam"]
    with pytest.raises(FileNotFoundError):
        Settings.load(tmp_path / "missing.yaml", env_file=None)
