"""Settings (from the environment / .env) and plant config (from stoplog.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODEL = "gpt-6.2-sol"

# Category a stop ends up in after normalisation. Order matters only for display.
CATEGORIES = [
    "breakdown",
    "minor_stop",
    "changeover",
    "planned",
    "material",
    "quality",
    "utility",
    "manpower",
    "unclassified",
]

# The type codes printed on the stoplog Line Stop Log form (see `stoplog print-form`).
# They follow the categories common downtime logs use (breakdown, minor stop, changeover,
# planned maintenance, material shortage, quality, utilities, manpower).
FORM_TYPE_CODES: list[tuple[str, str, str]] = [
    ("BD", "breakdown", "Breakdown (machine fault, {min}+ min)"),
    ("MS", "minor_stop", "Minor stop (jam, reset, under {min} min)"),
    ("CO", "changeover", "Changeover / SKU or size change"),
    ("PM", "planned", "Planned: cleaning, maintenance, breaks"),
    ("MAT", "material", "Material shortage (film, cartons, mass)"),
    ("QA", "quality", "Quality hold / rejection"),
    ("UT", "utility", "Utility: power, air, steam, water"),
    ("MP", "manpower", "Manpower: no operator"),
]

# What operators write in a "type" / "category" column, mapped to a category.
DEFAULT_CATEGORY_SYNONYMS: dict[str, list[str]] = {
    "breakdown": ["bd", "b/d", "b d", "breakdown", "break down", "failure", "fault", "repair"],
    "minor_stop": ["ms", "m/s", "minor", "minor stop", "minor stoppage", "small stop", "idle"],
    "changeover": ["co", "c/o", "changeover", "change over", "sku change", "size change",
                   "format change", "product change", "setup", "set up", "set-up"],
    "planned": ["pm", "planned", "planned maintenance", "preventive", "preventive maintenance",
                "cleaning", "cip", "lunch", "tea", "tea break", "meal break", "meeting", "trial"],
    "material": ["mat", "material", "no material", "material shortage", "shortage", "rm shortage",
                 "pm shortage", "waiting for material", "no film", "film over", "no stock"],
    "quality": ["quality", "qa", "qc", "quality hold", "rejection", "defect"],
    "utility": ["ut", "utility", "power", "power cut", "power failure", "eb", "air", "air pressure",
                "compressed air", "steam", "water"],
    "manpower": ["mp", "manpower", "no operator", "operator absent", "labour", "labor"],
}

# Phrases in the *reason* text that reveal a non-equipment category. Deliberately
# multi-word: a bare "break" would wrongly catch "film break".
DEFAULT_REASON_HINTS: dict[str, list[str]] = {
    "changeover": ["changeover", "change over", "sku change", "size change", "format change",
                   "product change"],
    "planned": ["planned maintenance", "preventive maintenance", "cleaning", "cip",
                "lunch break", "tea break", "meal break", "meeting"],
    "material": ["no material", "material shortage", "waiting for material", "no film",
                 "film over", "no stock", "no wrapper", "shortage of"],
    "utility": ["power cut", "power failure", "power trip", "air pressure", "low air",
                "steam pressure", "no steam", "no water"],
    "manpower": ["no operator", "operator absent", "manpower shortage"],
    "quality": ["quality hold", "qa hold", "qc hold"],
}

DEFAULT_REASON_GROUPS: dict[str, list[str]] = {
    "wrapper jam": ["wrap jam", "wrapping jam", "wrapper choke", "bar stuck in wrapper"],
    "film break": ["film tear", "film cut", "wrapper film break"],
    "reel change": ["roll change", "film change", "film reel change"],
    "stamper fault": ["stamping fault", "stamper jam", "die jam"],
    "cutter fault": ["cutter jam", "cutting fault"],
    "cartoner jam": ["carton jam", "cartoner fault"],
    "conveyor jam": ["conveyor stuck", "belt jam"],
    "sensor fault": ["sensor error", "photocell fault", "sensor issue"],
    "electrical fault": ["motor trip", "drive fault", "electrical issue"],
    "no material": ["material shortage", "no film", "no stock"],
    "sku changeover": ["sku change", "size change", "product change", "changeover"],
    "cleaning": ["cip", "line cleaning"],
    "power cut": ["power failure", "power trip", "eb cut"],
    "low air pressure": ["air pressure low", "no air"],
    "quality hold": ["qa hold", "qc hold"],
    "no operator": ["operator absent", "manpower shortage"],
}


def _parse_clock(value: str) -> time:
    hh, mm = str(value).strip().split(":")
    return time(int(hh) % 24, int(mm))


@dataclass(frozen=True)
class Shift:
    name: str
    start: time
    end: time

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    @property
    def start_min(self) -> int:
        return self.start.hour * 60 + self.start.minute

    @property
    def end_min(self) -> int:
        return self.end.hour * 60 + self.end.minute

    @property
    def minutes(self) -> int:
        span = (self.end_min - self.start_min) % 1440
        return span or 1440

    def contains(self, t: time, grace_min: int = 0) -> bool:
        """Is clock time `t` inside the shift (optionally widened by `grace_min` each side)?"""
        m = t.hour * 60 + t.minute
        offset = (m - (self.start_min - grace_min)) % 1440
        return offset < self.minutes + 2 * grace_min

    def label(self) -> str:
        return f"{self.name} ({self.start:%H:%M}-{self.end:%H:%M})"


DEFAULT_SHIFTS = (
    Shift("A", time(6, 0), time(14, 0)),
    Shift("B", time(14, 0), time(22, 0)),
    Shift("C", time(22, 0), time(6, 0)),
)


@dataclass
class PlantConfig:
    shifts: list[Shift] = field(default_factory=lambda: list(DEFAULT_SHIFTS))
    minor_stop_max_min: float = 10.0
    date_format: str = "DD/MM/YYYY"
    lines: list[str] = field(default_factory=list)
    machines: list[str] = field(default_factory=list)
    reason_groups: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_REASON_GROUPS))
    category_synonyms: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_CATEGORY_SYNONYMS.items()}
    )
    reason_hints: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_REASON_HINTS.items()}
    )

    def shift_by_name(self, name: str | None) -> Shift | None:
        if not name:
            return None
        for s in self.shifts:
            if s.name.lower() == str(name).strip().lower():
                return s
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlantConfig":
        data = data or {}
        cfg = cls()
        if "shifts" in data and data["shifts"]:
            shifts = []
            for item in data["shifts"]:
                shifts.append(Shift(str(item["name"]), _parse_clock(item["start"]), _parse_clock(item["end"])))
            cfg.shifts = shifts
        if "minor_stop_max_min" in data:
            cfg.minor_stop_max_min = float(data["minor_stop_max_min"])
        if "date_format" in data:
            cfg.date_format = str(data["date_format"]).upper()
        cfg.lines = [str(x) for x in data.get("lines") or []]
        cfg.machines = [str(x) for x in data.get("machines") or []]
        if data.get("reason_groups"):
            # A plant's own list replaces the defaults: its vocabulary is the point.
            cfg.reason_groups = {
                str(k).lower(): [str(s).lower() for s in (v or [])] for k, v in data["reason_groups"].items()
            }
        for key, value in (data.get("category_synonyms") or {}).items():
            if key not in CATEGORIES:
                raise ValueError(f"category_synonyms: unknown category {key!r}; use one of {CATEGORIES}")
            cfg.category_synonyms.setdefault(key, []).extend(str(s).lower() for s in value or [])
        return cfg


PROVIDERS = ("openai", "anthropic")
# Claude default is the small model at medium effort. On a real handwritten, sideways phone photo,
# Haiku 5.5 at low effort slipped on 3 fields (overwritten digit, crossed-out initials, an empty
# cell); medium read them all. Step up to claude-sonnet-5-5 if your handwriting reads worse.
DEFAULT_MODELS = {"openai": DEFAULT_MODEL, "anthropic": "claude-haiku-5-5"}


@dataclass
class Settings:
    provider: str = "auto"  # "openai", "anthropic", or "auto" (whichever key is set; OpenAI first)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    model: str | None = None  # None -> the provider's default
    insights_model: str | None = None  # None -> same as `model`
    effort: str = "medium"  # reading the photo (Claude: low|medium|high|xhigh|max)
    insights_effort: str = "low"  # writing the findings
    image_detail: str = "high"
    google_credentials: Path | None = None
    sheet_id: str | None = None
    webapp_url: str | None = None  # Apps Script bridge; a credential, so environment only
    store: str = "gsheet"  # "webapp", "gsheet" or "csv"
    csv_dir: Path = Path("stoplog-data")
    watch_dir: Path = Path("inbox")
    state_dir: Path = Path(".stoplog")
    poll_seconds: float = 10.0
    settle_seconds: float = 5.0
    max_attempts: int = 3
    narrative: bool = True
    plant: PlantConfig = field(default_factory=PlantConfig)

    def resolved_provider(self) -> str:
        """Which LLM provider to call. Raises with a fix-it message if none is usable."""
        p = (self.provider or "auto").lower()
        if p == "auto":
            if self.openai_api_key:
                return "openai"
            if self.anthropic_api_key:
                return "anthropic"
            raise RuntimeError(
                "No LLM key found. Set OPENAI_API_KEY (or ANTHROPIC_API_KEY with "
                "STOPLOG_PROVIDER=anthropic) in your environment or .env file."
            )
        if p not in PROVIDERS:
            raise RuntimeError(f"STOPLOG_PROVIDER must be one of {PROVIDERS} or 'auto', got {p!r}")
        key = self.openai_api_key if p == "openai" else self.anthropic_api_key
        if not key:
            raise RuntimeError(f"STOPLOG_PROVIDER={p} but {p.upper()}_API_KEY is not set.")
        return p

    def extraction_model(self) -> str:
        return self.model or DEFAULT_MODELS[self.resolved_provider()]

    def narrative_model(self) -> str:
        return self.insights_model or self.extraction_model()

    @classmethod
    def load(cls, config_path: str | os.PathLike | None = None, env_file: str | None = ".env") -> "Settings":
        if env_file:
            try:
                from dotenv import load_dotenv

                load_dotenv(env_file, override=False)
            except ImportError:  # pragma: no cover - dotenv is a declared dependency
                pass
        env = os.environ

        path = config_path or env.get("STOPLOG_CONFIG") or "stoplog.yaml"
        data: dict = {}
        if Path(path).exists():
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        elif config_path:
            raise FileNotFoundError(f"config file not found: {config_path}")
        plant = PlantConfig.from_dict(data)
        # Non-secret settings may live in the YAML under `settings:`; environment variables
        # win. Keys only ever come from the environment.
        ys = {str(k).lower(): v for k, v in (data.get("settings") or {}).items()}

        def get(env_name: str, yaml_key: str, default=None):
            value = env.get(env_name)
            if value not in (None, ""):
                return value
            value = ys.get(yaml_key)
            return default if value in (None, "") else str(value)

        creds = (env.get("GOOGLE_CREDENTIALS_FILE") or env.get("GOOGLE_APPLICATION_CREDENTIALS")
                 or ys.get("google_credentials_file"))
        return cls(
            provider=get("STOPLOG_PROVIDER", "provider", "auto").lower(),
            # STOPLOG_-prefixed names first: a machine-wide ANTHROPIC_API_KEY would also be
            # picked up by other tools (Claude Code among them).
            openai_api_key=env.get("STOPLOG_OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or None,
            anthropic_api_key=env.get("STOPLOG_ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY") or None,
            model=get("STOPLOG_MODEL", "model"),
            insights_model=get("STOPLOG_INSIGHTS_MODEL", "insights_model"),
            effort=get("STOPLOG_EFFORT", "effort", "medium").lower(),
            insights_effort=get("STOPLOG_INSIGHTS_EFFORT", "insights_effort", "low").lower(),
            image_detail=get("STOPLOG_IMAGE_DETAIL", "image_detail", "high"),
            google_credentials=Path(creds) if creds else None,
            sheet_id=_sheet_id(get("STOPLOG_SHEET_ID", "sheet_id")),
            webapp_url=(env.get("STOPLOG_WEBAPP_URL") or "").strip() or None,
            store=get("STOPLOG_STORE", "store", "gsheet").lower(),
            csv_dir=Path(get("STOPLOG_CSV_DIR", "csv_dir", "stoplog-data")),
            watch_dir=Path(get("STOPLOG_WATCH_DIR", "watch_dir", "inbox")),
            state_dir=Path(get("STOPLOG_STATE_DIR", "state_dir", ".stoplog")),
            poll_seconds=float(get("STOPLOG_POLL_SECONDS", "poll_seconds", 10)),
            settle_seconds=float(get("STOPLOG_SETTLE_SECONDS", "settle_seconds", 5)),
            max_attempts=int(get("STOPLOG_MAX_ATTEMPTS", "max_attempts", 3)),
            narrative=str(get("STOPLOG_NARRATIVE", "narrative", "1")).lower() not in ("0", "false", "no", "off"),
            plant=plant,
        )


def _sheet_id(value: str | None) -> str | None:
    """Accept either a bare spreadsheet id or a full docs.google.com URL."""
    if not value:
        return None
    value = value.strip()
    marker = "/spreadsheets/d/"
    if marker in value:
        return value.split(marker, 1)[1].split("/", 1)[0].split("?", 1)[0]
    return value
