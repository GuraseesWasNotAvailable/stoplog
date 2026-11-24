"""The printable Line Stop Log form (A4 landscape) that stoplog is tuned to read.

Its columns follow what common downtime / breakdown logs record (machine, stop and restart
time, minutes lost, a type code, reason, action taken, who wrote it), with the plant's shifts
and type-code legend printed on it. `stoplog print-form form.pdf` writes one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import FORM_TYPE_CODES, PlantConfig

A4_LANDSCAPE_MM = (297, 210)
ROWS = 15
# (key, label, relative width)
COLUMNS = [
    ("no", "No.", 0.035),
    ("machine", "Machine", 0.115),
    ("start", "Stop time", 0.075),
    ("end", "Restart time", 0.075),
    ("minutes", "Minutes", 0.065),
    ("type", "Type", 0.055),
    ("reason", "Reason (what happened)", 0.29),
    ("action", "Action taken", 0.22),
    ("initials", "Initials", 0.07),
]

_FONT_CANDIDATES = {
    False: ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf",
            "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Helvetica.ttc", "LiberationSans-Regular.ttf"],
    True: ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf",
           "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Helvetica.ttc", "LiberationSans-Bold.ttf"],
}


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for name in _FONT_CANDIDATES[bold]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


@dataclass
class FormLayout:
    """Pixel boxes of every fillable field, so a filled sample can be drawn into them."""

    size: tuple[int, int]
    header: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    shift_marks: dict[str, tuple[int, int, int]] = field(default_factory=dict)  # name -> (cx, cy, r)
    cells: list[dict[str, tuple[int, int, int, int]]] = field(default_factory=list)  # per row
    footer: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)


def render_form(plant: PlantConfig | None = None, dpi: int = 200) -> tuple[Image.Image, FormLayout]:
    plant = plant or PlantConfig()
    px = lambda mm: int(round(mm / 25.4 * dpi))  # noqa: E731
    W, H = px(A4_LANDSCAPE_MM[0]), px(A4_LANDSCAPE_MM[1])
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    lay = FormLayout(size=(W, H))
    ink, grey = (0, 0, 0), (110, 110, 110)
    m = px(10)
    s = dpi / 200  # scale factor for font sizes

    # corner marks help whoever frames the photo (and any later perspective fix)
    mk = px(5)
    for x, y in ((m - mk, m - mk), (W - m, m - mk), (m - mk, H - m), (W - m, H - m)):
        d.rectangle([x, y, x + mk, y + mk], fill=ink)

    title_f, label_f, small_f, cell_f = load_font(int(40 * s), True), load_font(int(19 * s)), \
        load_font(int(15 * s)), load_font(int(18 * s), True)
    y = m + px(2)
    d.text((m + px(2), y), "LINE STOP LOG", font=title_f, fill=ink)
    d.text((W - m - px(2), y + px(1)), "One row per stop. Times as 24-hour (14:05). Hand in at shift end.",
           font=small_f, fill=grey, anchor="ra")
    d.text((W - m - px(2), y + px(6)), "stoplog form v1", font=small_f, fill=grey, anchor="ra")

    # header fields
    y0, hh = m + px(15), px(13)
    specs = [("date", "Date (DD/MM/YYYY)", 0.17), ("shift", "Shift (circle one)", 0.19), ("line", "Line", 0.12),
             ("page", "Page", 0.07), ("sku", "Product / SKU", 0.21), ("incharge", "Shift in-charge", 0.24)]
    x = m
    usable = W - 2 * m
    for key, label, frac in specs:
        w = int(usable * frac)
        d.rectangle([x, y0, x + w, y0 + hh], outline=ink, width=2)
        d.text((x + px(1.5), y0 + px(1)), label, font=small_f, fill=grey)
        lay.header[key] = (x + px(1.5), y0 + px(5), x + w - px(1.5), y0 + hh - px(1))
        if key == "shift":
            n = max(1, len(plant.shifts))
            for i, sh in enumerate(plant.shifts):
                cx = x + int(w * (i + 0.5) / n)
                cy = y0 + px(8.5)
                d.text((cx, cy), sh.name, font=cell_f, fill=ink, anchor="mm")
                d.text((cx, cy + px(3.6)), f"{sh.start:%H:%M}-{sh.end:%H:%M}", font=load_font(int(11 * s)),
                       fill=grey, anchor="mm")
                lay.shift_marks[sh.name] = (cx, cy, px(3.8))
        x += w

    # table
    ty = y0 + hh + px(4)
    head_h = px(10)
    legend_h = px(30)
    row_h = (H - m - legend_h - ty - head_h) // ROWS
    xs = [m]
    for _, _, frac in COLUMNS:
        xs.append(xs[-1] + int(usable * frac))
    xs[-1] = W - m
    d.rectangle([m, ty, W - m, ty + head_h], fill=(232, 232, 232))
    for i, (key, label, _) in enumerate(COLUMNS):
        d.text(((xs[i] + xs[i + 1]) // 2, ty + head_h // 2), label, font=cell_f, fill=ink, anchor="mm")
    for r in range(ROWS):
        top = ty + head_h + r * row_h
        cells = {}
        for i, (key, _, _) in enumerate(COLUMNS):
            cells[key] = (xs[i] + px(1.2), top + px(1), xs[i + 1] - px(1.2), top + row_h - px(1))
        lay.cells.append(cells)
        d.text(((xs[0] + xs[1]) // 2, top + row_h // 2), str(r + 1), font=label_f, fill=grey, anchor="mm")
    bottom = ty + head_h + ROWS * row_h
    for r in range(ROWS + 1):
        yy = ty + head_h + r * row_h
        d.line([m, yy, W - m, yy], fill=ink, width=1 if 0 < r < ROWS else 2)
    for xx in xs:
        d.line([xx, ty, xx, bottom], fill=ink, width=2 if xx in (xs[0], xs[-1]) else 1)
    d.rectangle([m, ty, W - m, bottom], outline=ink, width=3)

    # legend + footer
    ly = bottom + px(3)
    d.text((m, ly), "Type codes", font=cell_f, fill=ink)
    per_col = (len(FORM_TYPE_CODES) + 1) // 2
    thr = int(plant.minor_stop_max_min) if float(plant.minor_stop_max_min).is_integer() else plant.minor_stop_max_min
    for i, (code, _, label) in enumerate(FORM_TYPE_CODES):
        cx = m + (i // per_col) * int(usable * 0.3)
        cy = ly + px(6) + (i % per_col) * px(5)
        d.text((cx, cy), code, font=cell_f, fill=ink)
        d.text((cx + px(16), cy), label.format(min=thr), font=small_f, fill=ink)
    nx = m + int(usable * 0.62)
    d.text((nx, ly), "Same short stop many times? Use one row: write it as", font=small_f, fill=grey)
    d.text((nx, ly + px(4.5)), "'x4' after the reason and the total minutes.", font=small_f, fill=grey)
    fy = ly + px(14)
    for key, label, off in (("total", "Total stop minutes", 0), ("checked", "Checked by", 0.19)):
        fx = nx + int(usable * off)
        d.text((fx, fy), label, font=small_f, fill=ink)
        d.line([fx, fy + px(9), fx + int(usable * 0.17), fy + px(9)], fill=ink, width=1)
        lay.footer[key] = (fx, fy + px(3), fx + int(usable * 0.17), fy + px(9))
    return img, lay


def save_form(path: str | Path, plant: PlantConfig | None = None, dpi: int = 200) -> Path:
    path = Path(path)
    img, _ = render_form(plant, dpi=dpi)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".pdf":
        img.save(path, "PDF", resolution=float(dpi))
    else:
        img.save(path)
    return path


if __name__ == "__main__":  # pragma: no cover
    out = save_form(sys.argv[1] if len(sys.argv) > 1 else "line-stop-log.pdf")
    print(out)
